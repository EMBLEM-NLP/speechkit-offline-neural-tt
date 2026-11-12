# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
# Building the command line for a backend and running `python -m speechkit_narrate --json ...`.

# Works out how to start `python -m speechkit_narrate --json <Arguments>` for a backend, without starting anything.
# -Arguments (string array) are the CLI words after --json, -Backend (Auto, Native or Wsl; empty = session setting),
# -ForRegistration asks for the stable form written into an MCP client's config. Returns a [pscustomobject] with
# Backend, FilePath, ArgumentList and Environment, which Invoke-NarrationCli, Start-NarrationMcpServer and
# Register-NarrationMcpServer turn into a process or a config entry. May start wsl.exe to translate the toolkit path.
function Get-NarrationLaunchSpec {
    # FilePath + ArgumentList + Environment that run `python -m speechkit_narrate --json <Arguments>` on a backend.
    # -ForRegistration: the stable form written into an MCP client config (no inherited PYTHONPATH).
    param(
        [string[]]$Arguments = @(),  # the speechkit-voice-tts-offline-pipeline command and its arguments, for example 'plan', 'C:\Books\my-book'
        [string]$Backend,  # backend name; empty means the session's backend
        [switch]$ForRegistration  # produce the entry for Claude Desktop's config rather than for an immediate run
    )
    if (-not $Backend) { $Backend = $script:Config.Backend }  # default to the session backend
    # throws early when the configured toolkit folder is missing; $null in Installed mode, where Python imports the
    # installed package and nothing goes on PYTHONPATH
    $root = Get-NarrationToolkitRoot
    if ((Get-NarrationEffectiveBackend $Backend) -eq 'Wsl') {  # Wsl backend: the whole toolkit runs inside Linux
        # wsl.exe exists only on Windows; $env:SPEECHKIT_WSL can supply a stand-in (the tests use one)
        if (-not (Test-NarrationWslAvailable)) {
            # explain what is missing instead of failing later with a cryptic start error
            throw "The Wsl backend needs wsl.exe (Windows Subsystem for Linux), or `$env:SPEECHKIT_WSL naming a stand-in."
        }
        $prefix = Get-NarrationWslPrefix  # wsl.exe [-d <distro>] --exec, or the override
        # `env` (the Linux program) sets these variables and then runs Python; --exec bypasses the Linux shell, so the
        # usual NAME=value prefix syntax would not work without it. The two UTF-8 settings make Python read and write
        # UTF-8 whatever the Linux locale is.
        $envArgs = @('PYTHONUTF8=1', 'PYTHONIOENCODING=utf-8')
        # Source mode: PYTHONPATH is the toolkit folder as WSL sees it (/mnt/c/...), listed first. Installed mode has
        # none, because the WSL python must already have the speechkit-voice-tts-offline-pipeline package installed.
        if ($root) { $envArgs = @("PYTHONPATH=$(ConvertTo-NarrationWslPath -Path $root)") + $envArgs }
        $plugins = Get-NarrationWslPluginValue  # the plug-in list with .py paths translated to WSL paths, or $null
        # pass plug-ins along, since Windows environment variables do not cross into WSL by default
        if ($plugins) { $envArgs += "SPEECHKIT_PLUGINS=$plugins" }
        # extra variables from Set-NarrationBackend -WslEnvironment, sorted so the command line is the same every time
        foreach ($k in @($script:Config.WslEnvironment.Keys | Sort-Object)) {
            $envArgs += "$k=$($script:Config.WslEnvironment[$k])"  # each one becomes NAME=value on the env command line
        }
        # launcher arguments, then env with its variables, then the WSL python running the speechkit_narrate package with --json,
        # then the caller's arguments
        $argList = @($prefix.Arguments) + @('env') + $envArgs + @((Resolve-NarrationWslPython), '-m', 'speechkit_narrate', '--json') + $Arguments
        return [pscustomobject]@{  # the WSL launch description
            Backend      = 'Wsl'  # lets callers know paths in the result must be mapped back to Windows
            FilePath     = $prefix.FilePath  # wsl.exe (or the override's program)
            ArgumentList = [string[]]$argList  # typed as a string array for ProcessStartInfo and the JSON config
            # makes wsl.exe write its own messages (such as "distribution not found") as UTF-8 rather than UTF-16
            Environment  = @{ WSL_UTF8 = '1' }
        }
    }
    $py = Resolve-NarrationPython  # native and Auto: the resolved local Python (throws when none is found)
    # ordered so the registered "env" block always lists the variables in the same order
    $environment = [ordered]@{}
    if ($root) {  # Source mode: the toolkit folder makes `python -m speechkit_narrate` importable
        $pythonPath = $root  # just the toolkit folder
        # for an immediate run keep any PYTHONPATH the user already has; the registered config must not depend on this
        # session
        if ($env:PYTHONPATH -and -not $ForRegistration) {
            # toolkit first so its speechkit_narrate package wins over anything else on the user's path; PathSeparator is ';' on
            # Windows and ':' elsewhere
            $pythonPath = $root + [System.IO.Path]::PathSeparator + $env:PYTHONPATH
        }
        # only in Source mode; Installed mode relies on the Python's site-packages
        $environment['PYTHONPATH'] = $pythonPath
    }
    $environment['PYTHONUTF8'] = '1'  # the UTF-8 settings mirror the WSL branch
    $environment['PYTHONIOENCODING'] = 'utf-8'  # stdin, stdout and stderr in UTF-8 whatever the console code page is
    # a running child inherits SPEECHKIT_PLUGINS anyway, but Claude Desktop does not, so the registration copies it in
    if ($ForRegistration -and $env:SPEECHKIT_PLUGINS) { $environment['SPEECHKIT_PLUGINS'] = $env:SPEECHKIT_PLUGINS }
    # in Auto mode on Windows: the command line Python should use to reach WSL, else $null
    $delegate = Get-NarrationDelegateCommand -Backend $Backend
    # set it when there is something to set; a user-set $env:SPEECHKIT_DELEGATE is already inherited, except by the
    # registered config
    if ($delegate -and ($ForRegistration -or -not $env:SPEECHKIT_DELEGATE)) {
        # tells the toolkit how to run Linux-only stages (narrate, autofix, audit) and the speak tool
        $environment['SPEECHKIT_DELEGATE'] = $delegate
        # Only commands that can actually delegate need the python inside WSL; finding it may start the WSL VM,
        # so plan/status/stages/settings do not look for it.
        # Only these subcommands can start a delegated stage or tool. Note that 'mcp' is not in the list, so a server
        # started with Start-NarrationMcpServer does not get SPEECHKIT_DELEGATE_PYTHON, while a registered one does.
        $mayDelegate = $ForRegistration -or (@('run', 'tool', 'all') -contains ($Arguments | Select-Object -First 1))
        # skip the probe when the user already chose the delegate's python explicitly
        if ($mayDelegate -and -not $env:SPEECHKIT_DELEGATE_PYTHON) {
            $wslPy = Resolve-NarrationWslPython  # the venv python made by Install-NarrationDependency -Wsl, or python3
            # python3 is the toolkit's own default (registry.py), so it is only spelled out when different
            if ($wslPy -and $wslPy -ne 'python3') { $environment['SPEECHKIT_DELEGATE_PYTHON'] = $wslPy }
        }
    }
    [pscustomobject]@{  # the native launch description
        Backend      = 'Native'  # runs on this machine (Auto delegates from inside Python, not here)
        FilePath     = $py.Executable  # full path of python.exe found by Resolve-NarrationPython
        # -m speechkit_narrate runs the package's __main__.py; --json makes it print one JSON result on stdout and logs on stderr
        ArgumentList = [string[]](@('-m', 'speechkit_narrate', '--json') + $Arguments)
        Environment  = $environment  # PYTHONPATH, UTF-8 settings and the delegation variables
    }
}

# Parses the JSON text Python printed into PowerShell objects (PSCustomObject, arrays, numbers, strings).
# -Json is the text. Returns the parsed object. Exists to paper over the difference between PowerShell editions.
function ConvertFrom-NarrationJson {
    # the JSON text, one complete document such as the last {...} line of stdout
    param([Parameter(Mandatory = $true)][string]$Json)
    # ConvertFrom-Json gained -Depth in PowerShell 6.2 (6.0 and 6.1 lack it, as does Windows PowerShell 5.1), so the
    # full version is compared, not just the major number; 100 levels is plenty for stage results inside run results
    if ($PSVersionTable.PSVersion -ge [version]'6.2') { ConvertFrom-Json -InputObject $Json -Depth 100 }
    else { ConvertFrom-Json -InputObject $Json }  # older engines use their built-in depth limit
}

# Converts a PowerShell value into plain data (ordered dictionaries, object arrays, strings, numbers, booleans) so that
# ConvertTo-Json produces the same JSON on Windows PowerShell 5.1 and PowerShell 7. -Value is any value: hashtables,
# PSCustomObjects, arrays, switches, FileInfo, dates or enums. Returns the converted value; recursive, no side effects.
# Used for --options, --set, --params and the Claude Desktop config.
function ConvertTo-NarrationPlain {
    # Hashtables, PSCustomObjects, arrays and switches -> plain ordered dictionaries / object[] / bool,
    # so ConvertTo-Json writes the same JSON on Windows PowerShell 5.1 and PowerShell 7.
    param($Value)  # anything; nested values are converted recursively
    if ($null -eq $Value) { return $null }  # null stays null (JSON null)
    # values that came through the pipeline are wrapped in a PSObject; BaseObject is the real .NET value underneath
    $base = $Value.PSObject.BaseObject
    # unwrap so the -is tests below see the real type; a PSCustomObject is kept because its properties live on the
    # wrapper
    if ($null -ne $base -and $base -isnot [System.Management.Automation.PSCustomObject]) { $Value = $base }
    # a switch becomes true or false instead of an object with an IsPresent property
    if ($Value -is [System.Management.Automation.SwitchParameter]) { return $Value.IsPresent }
    # strings and single characters are sent as plain text, never as character objects
    if ($Value -is [string] -or $Value -is [char]) { return [string]$Value }
    # a file or folder object from Get-Item becomes its full path rather than dozens of properties
    if ($Value -is [System.IO.FileSystemInfo]) { return $Value.FullName }
    # dates become ISO 8601 (2024-05-01T13:45:00) instead of the 5.1 "\/Date(...)\/" form
    if ($Value -is [datetime]) { return $Value.ToString('s') }
    if ($Value -is [enum]) { return $Value.ToString() }  # enums become their names, not their numeric values
    if ($Value -is [System.Collections.IDictionary]) {  # hashtables and ordered dictionaries
        $d = [ordered]@{}  # a fresh ordered dictionary keeps the caller's key order
        # JSON keys must be strings; each value is converted in turn
        foreach ($k in @($Value.Keys)) { $d[[string]$k] = ConvertTo-NarrationPlain $Value[$k] }
        return $d  # return the converted dictionary
    }
    # objects such as those made by ConvertFrom-Json or [pscustomobject]@{}
    if ($Value -is [System.Management.Automation.PSCustomObject]) {
        $d = [ordered]@{}  # collect their properties in order
        # property name becomes the key, its value is converted
        foreach ($p in $Value.PSObject.Properties) { $d[$p.Name] = ConvertTo-NarrationPlain $p.Value }
        return $d  # a PSCustomObject becomes an ordered dictionary with its properties in order
    }
    # arrays, lists and other sequences (strings were already handled above)
    if ($Value -is [System.Collections.IEnumerable]) {
        $list = New-Object System.Collections.Generic.List[object]  # a growable list of converted items
        # convert each element recursively, so nested objects and switches become plain too
        foreach ($item in $Value) { $list.Add((ConvertTo-NarrationPlain $item)) }
        # the comma stops PowerShell from unrolling an empty or single-item array, so JSON still gets [] or [x]
        return , $list.ToArray()
    }
    $Value  # numbers, booleans and anything else are already plain
}

# Turns a PowerShell value into compact one-line JSON for the Python side (--options, --set, --params).
# -Value is any value ConvertTo-NarrationPlain understands. Returns a string.
function ConvertTo-NarrationJson {
    param($Value)  # the value to serialise
    # depth 32 because ConvertTo-Json's default of 2 silently flattens nested settings to type names; -Compress keeps it
    # to one line for the command line
    ConvertTo-Json -InputObject (ConvertTo-NarrationPlain $Value) -Depth 32 -Compress
}

# Builds a PowerShell ErrorRecord from a failure reported by Python, so callers get a normal terminating error with
# a sensible category for -ErrorAction and $Error. -Message is the text, -Type the Python exception class name
# (ProjectError, RegistryError, ValueError and so on), -Target the object involved (usually the project path),
# -Trace Python's traceback text. Returns the ErrorRecord; its FullyQualifiedErrorId is SpeechKitVoiceTtsOfflinePipeline.<Type>.
function New-NarrationErrorRecord {
    # PSScriptAnalyzer asks for -WhatIf on New-* functions; this one only builds an in-memory ErrorRecord, so it is
    # exempt
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '',
        Justification = 'Builds an in-memory ErrorRecord only')]  # why the rule is waived
    param(  # all four inputs are optional strings except the free-form target
        [string]$Message,  # the message shown to the user
        [string]$Type = 'Error',  # Python's exception class, or a module-made name such as NoResult
        $Target,  # what the error is about, shown as TargetObject
        [string]$Trace  # optional traceback for debugging
    )
    $category = [System.Management.Automation.ErrorCategory]::NotSpecified  # used when no rule below matches
    switch -Regex ($Type) {  # -Regex lets each case be a pattern against the type name
        '^(ProjectError|FileNotFoundError)$' {  # problems with a project or an input file
            # "not found" style messages mean the object is missing; anything else is an invalid operation
            if ($Message -match 'not found|does not exist|no such') { $category = 'ObjectNotFound' } else { $category = 'InvalidOperation' }
        }
        # unknown stage names, bad option values and wrong types are argument errors
        '^(RegistryError|ValueError|TypeError|KeyError)$' { $category = 'InvalidArgument' }
        # Python timeouts (TimeoutError, TimeoutExpired) map to the timeout category
        '^Timeout' { $category = 'OperationTimeout' }
    }
    $ex = New-Object System.InvalidOperationException($Message)  # the exception that carries the message
    # keep the Python class name where scripts can test it: $_.Exception.Data['NarrationType']
    $ex.Data['NarrationType'] = $Type
    if ($Trace) { $ex.Data['Trace'] = $Trace }  # the traceback, when Python sent one
    # exception, error id, category and target make up the record
    New-Object System.Management.Automation.ErrorRecord($ex, "SpeechKitVoiceTtsOfflinePipeline.$Type", $category, $Target)
}

# The heart of the module: runs `python -m speechkit_narrate --json <Arguments>` on the chosen backend and returns Python's
# one-line JSON result as an object. Log lines (stderr) stream live to Write-Verbose; each stage start also updates a
# progress bar and the information stream. When the result has ok = false a terminating error carrying Python's
# message and type is thrown unless -AllowFailure is given. The project, stage, tool, settings and environment
# commands call this. Get-/Set-NarrationBackend (Public\Backend.ps1) never run the CLI, and Start-/Register-
# NarrationMcpServer (Public\Mcp.ps1) build their command line with Get-NarrationLaunchSpec instead.
function Invoke-NarrationCli {
    # Runs `python -m speechkit_narrate --json <Arguments>` on the chosen backend. stderr log lines stream live to
    # Write-Verbose; stage starts ("run: <stage> (...)") also go to Write-Progress and Write-Information.
    # Returns the stdout JSON object; throws a terminating error carrying the Python message when ok is false.
    [CmdletBinding()]
    param(  # the common parameters (-Verbose, -Debug, ...) work and $PSCmdlet is available
        [Parameter(Mandatory = $true)][string[]]$Arguments,  # the speechkit-voice-tts-offline-pipeline subcommand and its arguments
        [string]$Backend,  # backend override; empty means the session's backend
        [switch]$AllowFailure,  # return the result even when ok is false (the doctor reports failures as data)
        [string]$Activity = 'speechkit-voice-tts-offline-pipeline',  # title of the progress bar
        [int]$Total = 0,  # number of stages expected to run, for the percentage; 0 means unknown
        $Target  # attached to any error as its TargetObject
    )
    $spec = Get-NarrationLaunchSpec -Arguments $Arguments -Backend $Backend  # how to start Python for this backend
    # visible with -Debug: the exact command line
    Write-Debug "speechkit-voice-tts-offline-pipeline: $($spec.FilePath) $(ConvertTo-NarrationArgumentString $spec.ArgumentList)"
    # captured mode: the JSON comes from stdout, the log from stderr
    $psi = New-NarrationStartInfo -FilePath $spec.FilePath -ArgumentList $spec.ArgumentList -Environment $spec.Environment -Redirect
    # launch Python (or wsl.exe); a launch failure throws a message naming the program
    $proc = Start-NarrationProcess -StartInfo $psi
    # the last 40 log lines, used when Python dies without a result
    $tail = New-Object System.Collections.Generic.List[string]
    $started = 0  # how many stages have started so far
    $stdout = ''  # the full stdout text once Python finishes
    # a fixed, arbitrary id so this bar can be updated and closed without touching any progress bar the caller shows
    $progressId = 4711
    try {  # the finally block ends the process and the bar no matter what happens
        $proc.StandardInput.Close()  # no input is ever sent; end-of-file at once prevents a hang on a prompt
        # stdout is read in the background while stderr is read below, so neither pipe can fill up and block Python
        $outTask = $proc.StandardOutput.ReadToEndAsync()
        # read the log line by line as it is written; ReadLine returns $null when Python closes stderr
        while ($null -ne ($line = $proc.StandardError.ReadLine())) {
            # drop NUL characters (UTF-16 output from wsl.exe) and trailing spaces or carriage returns
            $line = $line.Replace([string][char]0, '').TrimEnd()
            if (-not $line) { continue }  # a line left empty after the cleanup carries no log text, so skip it
            # remove Python's "[12:34:56] " timestamp; PowerShell's streams have their own context
            $msg = $line -replace '^\[\d\d:\d\d:\d\d\] ', ''
            $tail.Add($msg)  # remember the line for error messages
            if ($tail.Count -gt 40) { $tail.RemoveAt(0) }  # keep only the newest 40
            Write-Verbose $msg  # the log appears with -Verbose
            # registry.run logs "run: <stage> (<reasons>)" as each stage starts; "run: stopped after ..." does not match
            # because no parenthesis follows the first word
            if ($msg -match '^run: (?<stage>[^\s(]+) \((?<why>.*)\)$') {
                $started++  # one more stage started
                $status = "$($Matches['stage']): $($Matches['why'])"  # for example "print: never ran"
                $pct = -1  # -1 means no percentage, used when the total is unknown
                # share of the stages finished before this one, capped at 99 so the bar never looks complete while work
                # continues
                if ($Total -gt 0) { $pct = [math]::Min(99, [int](100 * ($started - 1) / $Total)) }
                # update the bar under this command's id
                Write-Progress -Id $progressId -Activity $Activity -Status $status -PercentComplete $pct
                # the same news as an information record with tags, for -InformationVariable or -InformationAction
                # Continue
                Write-Information -MessageData "speechkit-voice-tts-offline-pipeline: running $status" -Tags 'SpeechKitVoiceTtsOfflinePipeline', 'Stage'
            }
        }
        $proc.WaitForExit()  # stderr is closed, now wait for the process itself
        $stdout = $outTask.Result  # the background read has finished; this is everything Python printed on stdout
    }
    finally {  # runs on success, failure and Ctrl+C
        Stop-NarrationProcess -Process $proc  # kill Python if it is still running (only after Ctrl+C or an exception)
        Write-Progress -Id $progressId -Activity $Activity -Completed  # remove the progress bar
    }
    $exit = $proc.ExitCode  # exit status, used in messages
    $proc.Dispose()  # release the process handle
    # the result is the last stdout line that starts with '{'; cli.py routes everything else to stderr, this just
    # tolerates stray output
    $jsonLine = @($stdout -split "`r?`n" | Where-Object { $_.TrimStart().StartsWith('{') }) | Select-Object -Last 1
    $command = ($Arguments | Select-Object -First 1)  # the subcommand name, used in messages
    if (-not $jsonLine) {  # no JSON at all: Python crashed, the wrong interpreter ran, or wsl.exe failed to start
        # the newest dozen log lines usually explain why
        $detail = ($tail | Select-Object -Last 12) -join [Environment]::NewLine
        if (-not $detail) { $detail = $stdout.Trim() }  # no log either: fall back to whatever stdout held
        $hint = Get-NarrationMissingPackageHint -Text $detail  # in Installed mode: how to install the missing package
        $message = "speechkit-voice-tts-offline-pipeline $command exited with code $exit without a result. $detail".Trim()  # what happened
        if ($hint) { $message += [Environment]::NewLine + $hint.Trim() }  # the advice goes on a line of its own
        # type NoResult so callers can tell this case apart from a reported error
        $er = New-NarrationErrorRecord -Message $message -Type 'NoResult' -Target $Target
        # ThrowTerminatingError reports the failure as coming from the command the user ran, not from this helper
        $PSCmdlet.ThrowTerminatingError($er)
    }
    try { $obj = ConvertFrom-NarrationJson -Json $jsonLine }  # parse the result line
    catch {  # the line looked like JSON but was not
        # include the text so the problem can be seen
        $er = New-NarrationErrorRecord -Message "speechkit-voice-tts-offline-pipeline $command printed output that is not JSON: $jsonLine" -Type 'BadResult' -Target $Target
        $PSCmdlet.ThrowTerminatingError($er)  # report it as a terminating error from the calling command
    }
    # with the Wsl backend, map /mnt/c/... paths in the result back to the Windows paths the user gave
    if ($spec.Backend -eq 'Wsl') { $obj = ConvertFrom-NarrationWslOutput -Object $obj }
    # Python reported a failure and the caller did not ask to receive failures as data
    if (-not $obj.ok -and -not $AllowFailure) {
        $type = 'Error'  # fallback type when Python sent none
        if ($obj.type) { $type = [string]$obj.type }  # Python's exception class name
        $message = [string]$obj.error  # the error text Python put in the reply's error field
        # generic message if Python sent no text
        if (-not $message) { $message = "speechkit-voice-tts-offline-pipeline $command failed (exit $exit)" }
        # unexpected Python exceptions include a traceback; show it with -Debug
        if ($obj.trace) { Write-Debug "speechkit-voice-tts-offline-pipeline traceback:`n$($obj.trace)" }
        # stop with Python's message, category and traceback attached
        $PSCmdlet.ThrowTerminatingError((New-NarrationErrorRecord -Message $message -Type $type -Target $Target -Trace ([string]$obj.trace)))
    }
    $obj  # the parsed result object
}

# Turns -Plugin values into --plugin=<spec> arguments for the CLI. A .py file (or any existing file) is made absolute
# against the current location and translated for the backend; a Python module name passes through unchanged.
# -Plugin is a string array, -Backend the backend name. Emits one string per plug-in.
function Get-NarrationPluginArgument {
    # --plugin=<spec> flags; .py files are resolved and translated for the backend.
    param([string[]]$Plugin, [string]$Backend)  # plug-in specs and the backend they are for
    foreach ($p in $Plugin) {  # each plug-in spec: a .py path or an importable module name
        if (-not $p) { continue }  # an empty or null spec, such as an empty array element, adds no flag
        # treat it as a file when it ends in .py or names a file that exists
        if ($p.EndsWith('.py') -or (Test-Path -LiteralPath $p -PathType Leaf)) {
            # absolute path, converted to /mnt/... for the Wsl backend
            "--plugin=$(ConvertTo-NarrationBackendPath -Path (Resolve-NarrationPath $p) -Backend $Backend)"
        }
        else { "--plugin=$p" }  # a module name stays as it is
    }
}

# Builds the planner flags that `plan` and `run` share from the matching PowerShell parameters. -Force, -Only and
# -Skip are string arrays of stage names, -From a stage name, -NoAdopt a switch. Emits CLI arguments in the
# --name=value form, so a value can never be mistaken for another option and stays one argument.
function Get-NarrationRunArgument {
    # The planner flags shared by plan and run.
    param([string[]]$Force, [string[]]$Only, [string[]]$Skip, [string]$From, [switch]$NoAdopt)
    # run these stages even if up to date ('all' for every stage); one flag per stage, empty names skipped
    foreach ($s in $Force) { if ($s) { "--force=$s" } }
    foreach ($s in $Only) { if ($s) { "--only=$s" } }  # run only these stages, without what they need
    foreach ($s in $Skip) { if ($s) { "--skip=$s" } }  # leave these stages out
    if ($From) { "--from=$From" }  # force this stage and everything after it
    if ($NoAdopt) { '--no-adopt' }  # do not adopt outputs that existed before tracking began
}

# Reads the stage registry with `python -m speechkit_narrate --json stages [DIR]`: stages, tools, plug-ins and the delegate.
# -Project is an absolute project folder (its own plug-ins are then included), -Plugin extra plug-ins, -Cached allows
# reuse of a result less than 60 seconds old. Returns the parsed registry object. Each fetch is stored in
# $script:Cache.Registry.
function Get-NarrationRegistry {
    # `stages [DIR]`: stages, tools, plug-ins of the registry (optionally with a project's own plug-ins).
    # -Cached reuses a result younger than a minute (tab completion and dynamic parameters).
    param([string]$Project, [string[]]$Plugin, [switch]$Cached)
    # everything that changes the registry's content is part of the key
    $key = "$($script:Config.Backend)|$Project|$($Plugin -join ';')|$($env:SPEECHKIT_PLUGINS)"
    # tab completion and dynamic parameters run on every key press, so they may use the cache
    if ($Cached -and $script:Cache.Registry.ContainsKey($key)) {
        $hit = $script:Cache.Registry[$key]  # the stored entry, with its time
        # 60 seconds: fast enough for typing, yet edits to a plug-in still show up within a minute
        if (((Get-Date) - $hit.Time).TotalSeconds -lt 60) { return $hit.Value }
    }
    $cliArgs = @('stages')  # the stages subcommand returns the registry: stages, tools and plug-ins
    # the project folder in the backend's form, when one was given
    if ($Project) { $cliArgs += (ConvertTo-NarrationBackendPath -Path $Project) }
    $cliArgs += @(Get-NarrationPluginArgument -Plugin $Plugin)  # plug-ins for this call
    $r = Invoke-NarrationCli -Arguments $cliArgs -Target $Project  # run it; errors name the project
    $script:Cache.Registry[$key] = @{ Time = Get-Date; Value = $r }  # remember the answer with the time it was fetched
    $r  # the freshly fetched registry object goes back to the caller
}

# Returns the standalone tools (`python -m speechkit_narrate --json tools`) as an array of tool descriptions: name,
# description, params (JSON schema), platforms, heavy, source and runs_here. -Plugin adds plug-ins for this call;
# -Refresh asks Python again instead of reusing the cached answer for this backend and plug-in set.
function Get-NarrationToolList {
    # `tools`, cached per backend and plug-in set.
    param([string[]]$Plugin, [switch]$Refresh)
    # the tool set depends on the backend and on which plug-ins are loaded
    $key = "$($script:Config.Backend)|$($Plugin -join ';')|$($env:SPEECHKIT_PLUGINS)"
    # reuse the earlier answer unless a refresh was requested
    if (-not $Refresh -and $script:Cache.Tools.ContainsKey($key)) { return $script:Cache.Tools[$key] }
    # the tools subcommand plus any --plugin flags
    $cliArgs = @('tools') + @(Get-NarrationPluginArgument -Plugin $Plugin)
    $r = Invoke-NarrationCli -Arguments $cliArgs  # run Python's tools command; a failure throws a terminating error
    $tools = @($r.tools)  # the array of tool descriptions; @() keeps a single tool as an array
    $script:Cache.Tools[$key] = $tools  # cache it under the key
    $tools  # emit the tool descriptions; PowerShell unrolls the array into the pipeline
}

# Resolves a -Path argument to an absolute folder path (string). A thin wrapper over Resolve-NarrationPath;
# nothing in the module calls it at present.
function Get-NarrationProjectPath {
    # Resolves -Path to an absolute folder (for messages and for the backend).
    param([Parameter(Mandatory = $true)][string]$Path)
    Resolve-NarrationPath $Path  # the same resolver every command uses
}
