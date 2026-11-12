# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
# JSON-schema properties -> PowerShell parameters (generated tool commands and stage dynamic parameters),
# and PowerShell values -> JSON values for the Python side.

# Names PowerShell gives every advanced function (the common parameters). A schema property may not reuse them,
# so Get-NarrationParameterSpec renames any property that would collide (for example a 'debug' option).
$script:CommonParameterNames = @(
    'Verbose', 'Debug', 'ErrorAction', 'WarningAction', 'InformationAction', 'ProgressAction', 'ErrorVariable',
    'WarningVariable', 'InformationVariable', 'OutVariable', 'OutBuffer', 'PipelineVariable', 'WhatIf', 'Confirm',
    'UseTransaction')
# Automatic variables a parameter must not be named after (the parameter would shadow or be shadowed by them).
# renaming away from these avoids a parameter such as -Input fighting with the automatic $Input variable
$script:ReservedVariableNames = @(
    'Input', 'Args', 'This', 'PSItem', 'Error', 'Host', 'Home', 'PID', 'PWD', 'ShellId', 'Matches', 'ForEach',
    'Switch', 'Null', 'True', 'False', 'PSBoundParameters', 'MyInvocation', 'PSCmdlet', 'ExecutionContext',
    'Event', 'EventArgs', 'EventSubscriber', 'Sender', 'LastExitCode', 'StackTrace', 'PSScriptRoot', 'PSCommandPath',
    'OFS', 'Profile', 'PSHome', 'PSVersionTable', 'IsWindows', 'IsLinux', 'IsMacOS', 'IsCoreCLR', 'PSEdition',
    'PSCulture', 'PSUICulture', 'NestedPromptLevel', 'ConsoleFileName', 'PSSenderInfo', 'PSDebugContext')

# Turns a schema or tool name such as 'text_file' or 'word-count' into PascalCase ('TextFile',
# 'WordCount') for PowerShell parameter and command names. -Name is the original string. Returns a string; a result
# starting with a digit gets a 'P' prefix because PowerShell names cannot start with one. No side effects.
function ConvertTo-NarrationPascalCase {
    param([string]$Name)  # the snake_case or kebab-case name
    # split on any run of characters that are not letters or digits, dropping empty pieces
    $parts = @($Name -split '[^A-Za-z0-9]+' | Where-Object { $_ })
    # capitalise the first letter of each piece, keep the rest as written, and glue the pieces together
    $s = ($parts | ForEach-Object { $_.Substring(0, 1).ToUpperInvariant() + $_.Substring(1) }) -join ''
    if ($s -match '^[0-9]') { $s = 'P' + $s }  # parameter names may not begin with a digit
    $s  # output the joined name, for example OutPrefix for out_prefix or P3d for 3d
}

# Reads one key from a JSON-schema node (a PSCustomObject from ConvertFrom-Json) and returns its value, or $null
# when the node or the key is missing. -Schema is the node, -Key the key name. Reading through PSObject.Properties
# never fails on an absent key, even under Set-StrictMode, which plain dot access would.
function Get-NarrationSchemaProperty {
    # Reads a key from a schema node (PSCustomObject from JSON) without failing when it is absent.
    param($Schema, [string]$Key)
    if ($null -eq $Schema) { return $null }  # no node, no value
    $p = $Schema.PSObject.Properties[$Key]  # property lookup by name; $null when absent
    # a present key yields its value even when that is false or 0; a missing key yields $null
    if ($p) { $p.Value } else { $null }
}

# Chooses the PowerShell type for a schema node. -Schema is the node. Returns a hashtable with Name (the type as
# written in generated source, such as 'string[]'), Type (the [type] object for runtime parameters) and Switch (true
# for booleans). Union types such as ["number", "string"] become string, the widest choice, and the value is turned
# back into a number later when it looks like one.
function Get-NarrationSchemaType {
    # -> @{ Name = 'string'; Type = [string]; Switch = $false } for a schema node.
    param($Schema)
    # 'type' may be one string or a list of strings; @() makes it a list and empty entries are dropped
    $t = @(Get-NarrationSchemaProperty $Schema 'type' | Where-Object { $_ })
    $single = $null  # the single type to map, once decided
    if ($t.Count -eq 1) { $single = [string]$t[0] }  # one declared type, such as 'integer', is used directly
    elseif ($t.Count -gt 1) {  # a union of several types
        # a string can carry any of the alternatives (for example "auto" or a number)
        if ($t -contains 'string') { $single = 'string' }
        elseif ($t -contains 'number') { $single = 'number' }  # otherwise prefer a fractional number
        elseif ($t -contains 'integer') { $single = 'integer' }  # then a whole number
    }
    switch ($single) {  # map the chosen JSON type
        # plain text maps to a [string] parameter that takes a value
        'string' { return @{ Name = 'string'; Type = [string]; Switch = $false } }
        'integer' { return @{ Name = 'int'; Type = [int]; Switch = $false } }  # JSON integer becomes a 32-bit [int]
        'number' { return @{ Name = 'double'; Type = [double]; Switch = $false } }  # JSON number becomes [double]
        # booleans become switches, so -Force means true
        'boolean' { return @{ Name = 'switch'; Type = [System.Management.Automation.SwitchParameter]; Switch = $true } }
        # JSON objects are given as hashtables, @{ key = value }
        'object' { return @{ Name = 'hashtable'; Type = [hashtable]; Switch = $false } }
        'array' {  # lists take their element type from the 'items' schema
            # the schema describing each list element, including the element's own 'type'
            $items = Get-NarrationSchemaProperty $Schema 'items'
            # pick the typed array that matches the element type
            switch ([string](Get-NarrationSchemaProperty $items 'type')) {
                # a list of text, such as quick's targets, becomes [string[]]
                'string' { return @{ Name = 'string[]'; Type = [string[]]; Switch = $false } }
                'integer' { return @{ Name = 'int[]'; Type = [int[]]; Switch = $false } }  # list of whole numbers
                # list of fractional numbers (such as a size given as 6, 9)
                'number' { return @{ Name = 'double[]'; Type = [double[]]; Switch = $false } }
                # mixed or unknown elements
                default { return @{ Name = 'object[]'; Type = [object[]]; Switch = $false } }
            }
        }
    }
    @{ Name = 'object'; Type = [object]; Switch = $false }  # no usable type: accept any value
}

# Builds one parameter description per JSON-schema property, ready to become a generated parameter or a dynamic one.
# -Properties is the schema's properties object, -Required the names that must be given, -Taken names the command
# already uses. Returns an array of [pscustomobject] specs (Key, Name, Alias, TypeName, Type, Switch, Mandatory,
# IsPath, Enum, Minimum, Maximum, Description, HasDefault, Default, Schema). Used by New-NarrationToolFunctionText
# and by Invoke-NarrationStage. No side effects.
function Get-NarrationParameterSpec {
    # One PowerShell parameter per schema property: name (snake_case -> PascalCase, renamed away from
    # automatic variables, common parameters and -Taken names), type, ValidateSet, ValidateRange, help text.
    param(
        $Properties,  # the 'properties' object of the schema, or $null
        [string[]]$Required = @(),  # property names listed under 'required'
        [string[]]$Taken = @()  # parameter names the host command already has
    )
    # names already in use; case-insensitive because PowerShell parameter names are
    $used = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($t in $Taken) { [void]$used.Add($t) }  # reserve the host command's names
    foreach ($t in $script:CommonParameterNames) { [void]$used.Add($t) }  # and the common parameters
    $specs = New-Object System.Collections.Generic.List[object]  # the specs being built
    # no properties: an empty array, kept as an array by the comma
    if ($null -eq $Properties) { return , $specs.ToArray() }
    foreach ($prop in $Properties.PSObject.Properties) {  # each property in the order the schema declares it
        $schema = $prop.Value  # the property's JSON-schema node: type, enum, default, description and so on
        $pascal = ConvertTo-NarrationPascalCase $prop.Name  # the natural PowerShell name
        # properties with "format": "path" are files or folders
        $isPath = ((Get-NarrationSchemaProperty $schema 'format') -eq 'path')
        $name = $pascal  # the name actually used
        $alias = @()  # extra names accepted for the same parameter
        # the natural name clashes with an automatic variable, a common parameter or a name already taken
        if ($script:ReservedVariableNames -contains $pascal -or $script:CommonParameterNames -contains $pascal -or $used.Contains($pascal)) {
            # rename with a suffix: 'input' becomes InputPath for a path, other names get Value
            if ($isPath) { $name = $pascal + 'Path' } else { $name = $pascal + 'Value' }
            # keep the natural spelling as an alias when it is still free (never for common parameters)
            if ($script:CommonParameterNames -notcontains $pascal -and -not $used.Contains($pascal)) { $alias += $pascal }
        }
        $n = 2  # the first numbered suffix to try
        $baseName = $name  # the name before numbering
        # a renamed name that still clashes gets 2, 3, ... appended
        while ($used.Contains($name)) { $name = "$baseName$n"; $n++ }
        [void]$used.Add($name)  # reserve the final name
        foreach ($a in $alias) { [void]$used.Add($a) }  # reserve the aliases too
        $type = Get-NarrationSchemaType $schema  # type mapping for this property
        # allowed values as strings, for ValidateSet and the help text
        $enum = @(Get-NarrationSchemaProperty $schema 'enum' | Where-Object { $null -ne $_ } | ForEach-Object { [string]$_ })
        $default = Get-NarrationSchemaProperty $schema 'default'  # the documented default, if any
        # whether a default is declared at all; a default of false or 0 still counts
        $hasDefault = $null -ne $schema.PSObject.Properties['default']
        $specs.Add([pscustomobject]@{  # record one spec object; its fields are listed one per line below
                Key         = $prop.Name  # the schema's own name, used when sending values back to Python
                Name        = $name  # the PowerShell parameter name
                Alias       = [string[]]$alias  # the natural name kept as an alias after a rename, or an empty list
                TypeName    = $type.Name  # type as written in generated source
                Type        = $type.Type  # type object for a runtime parameter
                Switch      = $type.Switch  # true for JSON booleans, which become switch parameters such as -AcceptDraft
                # true when the schema's required list names this key; generated commands then insist on it
                Mandatory   = ($Required -contains $prop.Name)
                IsPath      = $isPath  # path values are resolved before they are sent
                Enum        = [string[]]$enum  # allowed values, possibly empty
                Minimum     = Get-NarrationSchemaProperty $schema 'minimum'  # lower bound, or $null
                Maximum     = Get-NarrationSchemaProperty $schema 'maximum'  # upper bound, or $null
                # the schema's description text
                Description = [string](Get-NarrationSchemaProperty $schema 'description')
                HasDefault  = $hasDefault  # whether to mention a default in the help
                Default     = $default  # the default value itself
                Schema      = $schema  # the full node, used when converting values
            })
    }
    , $specs.ToArray()  # return the specs as one array
}

# Writes the help sentence for one parameter: the schema description plus notes on paths, allowed values, the
# default, how to turn off a switch that defaults to on, and whether it is required. -Spec is a spec from
# Get-NarrationParameterSpec. Returns a string.
function Get-NarrationParameterHelp {
    param($Spec)  # the spec whose description, enum, default and flags are turned into text
    $text = $Spec.Description.Trim()  # start from the schema's description
    if (-not $text) { $text = "The '$($Spec.Key)' value." }  # no description: a neutral sentence naming the key
    elseif ($text -notmatch '[.!?)]$') { $text += '.' }  # end the sentence with punctuation if it lacks any
    $extra = @()  # the notes that follow
    # paths are resolved against the current location before Python sees them
    if ($Spec.IsPath) { $extra += 'A path; relative paths are resolved against the current location.' }
    if ($Spec.Enum.Count) { $extra += "One of: $($Spec.Enum -join ', ')." }  # list the allowed values
    if ($Spec.HasDefault) {  # a default is declared
        $d = $Spec.Default  # the value to print
        if ($d -is [bool]) { $d = ('$' + "$d".ToLowerInvariant()) }  # booleans in PowerShell spelling, $true or $false
        # a list default such as a size reads '6, 9' instead of PowerShell's space-joined form
        elseif ($d -is [System.Array]) { $d = ($d -join ', ') }
        $extra += "Default: $d."  # add a sentence like 'Default: letter.' to the help notes
        # a switch that is on by default can only be turned off with -Name:$false, so say so
        if ($Spec.Switch -and $Spec.Default -eq $true) { $extra += "Pass -$($Spec.Name):`$false to turn it off." }
    }
    if ($Spec.Mandatory) { $extra += 'Required.' }  # mandatory parameters say so
    (@($text) + $extra) -join ' '  # description first, then the notes, separated by spaces
}

# Converts one bound PowerShell value into the JSON value the schema expects: switches to booleans, FileInfo objects
# to paths, path strings to absolute (and WSL) paths, numeric text to numbers. Arrays are converted item by item with
# the 'items' schema. -Value is the value, -Schema the property's schema node ($null = no conversion), -Backend the
# backend name for path translation. Returns the converted value. May start wsl.exe to translate paths.
function ConvertTo-NarrationSchemaValue {
    # A bound PowerShell value -> the JSON value the schema expects: switches to booleans, paths made
    # absolute (and translated for WSL), numeric strings to numbers where the schema allows numbers.
    param($Value, $Schema, [string]$Backend)
    if ($null -eq $Value) { return $null }  # an unbound or null value goes to Python as JSON null
    # a switch becomes true or false
    if ($Value -is [System.Management.Automation.SwitchParameter]) { return $Value.IsPresent }
    # a file or folder object (for example from Get-Item) becomes its full path
    if ($Value -is [System.IO.FileSystemInfo]) { $Value = $Value.FullName }
    if ($null -eq $Schema) { return $Value }  # without a schema the value is sent as it is
    $types = @(Get-NarrationSchemaProperty $Schema 'type')  # the declared type or types, as a list
    if ($types -contains 'array') {  # the schema expects a list, so each element is converted on its own
        $items = Get-NarrationSchemaProperty $Schema 'items'  # the schema for each element
        # a growable list that collects each converted element
        $list = New-Object System.Collections.Generic.List[object]
        # @() also accepts a single value where a list is expected
        foreach ($v in @($Value)) { $list.Add((ConvertTo-NarrationSchemaValue -Value $v -Schema $items -Backend $Backend)) }
        return , $list.ToArray()  # the comma keeps a one-item list as a list
    }
    # a non-empty path string
    if ((Get-NarrationSchemaProperty $Schema 'format') -eq 'path' -and $Value -is [string] -and $Value) {
        # made absolute against the current location, then translated for the backend
        return ConvertTo-NarrationBackendPath -Path (Resolve-NarrationPath $Value) -Backend $Backend
    }
    # numeric text for a schema that takes numbers (for example a number given as '0.1')
    if ($Value -is [string] -and ($types -contains 'number' -or $types -contains 'integer')) {
        $d = 0.0  # receives the parsed number
        # InvariantCulture so '0.5' parses the same on systems whose decimal mark is a comma
        if ([double]::TryParse($Value, [System.Globalization.NumberStyles]::Float, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$d)) {
            # an integer-only schema gets a whole number as [long], so the JSON shows 4 rather than 4.0
            if ($types -notcontains 'number' -and [math]::Floor($d) -eq $d) { return [long]$d }
            return $d  # otherwise a fractional number
        }
    }
    $Value  # everything else is already in the right shape
}

# Maps a hashtable of parameters onto a schema's property names and converts every value. Keys may be the schema's
# snake_case names or the PascalCase names the generated commands use; out_prefix, OutPrefix and out-prefix all match.
# -Parameters is the hashtable, -Properties the schema's properties object, -Backend the backend name.
# Returns an ordered dictionary keyed by the schema's names. Used by Invoke-NarrationTool and Invoke-NarrationStage.
function ConvertTo-NarrationParameterSet {
    # A hashtable of parameters (snake_case keys, or PascalCase as the generated commands use) -> an ordered
    # dictionary keyed by the schema's own names, values converted with ConvertTo-NarrationSchemaValue.
    param([System.Collections.IDictionary]$Parameters, $Properties, [string]$Backend)
    $names = @()  # the property names the schema declares; stays empty without a schema
    # read them from the properties object when there is one
    if ($Properties) { $names = @($Properties.PSObject.Properties | ForEach-Object { $_.Name }) }
    $out = [ordered]@{}  # an ordered dictionary so keys reach Python in the caller's order
    if ($null -eq $Parameters) { return $out }  # no parameters were passed: return the empty dictionary
    foreach ($key in @($Parameters.Keys)) {  # each key the caller used; @() copies the key list
        $k = [string]$key  # the key as text
        $canon = $null  # the matching schema name, once found
        # exact match first (-eq ignores case in PowerShell)
        foreach ($n in $names) { if ($n -eq $k) { $canon = $n; break } }
        if (-not $canon) {  # the key differs from every schema name, so try a looser spelling match
            $flat = ($k -replace '[_\-]', '')  # drop underscores and hyphens from the caller's key
            # and compare against schema names without underscores
            foreach ($n in $names) { if (($n -replace '_', '') -eq $flat) { $canon = $n; break } }
        }
        # unknown keys are passed on untouched; Python reports them with its own message
        if (-not $canon) { $canon = $k }
        $schema = $null  # the schema for this key, if any
        # look it up by the canonical name
        if ($Properties -and $Properties.PSObject.Properties[$canon]) { $schema = $Properties.PSObject.Properties[$canon].Value }
        # store the converted value under the schema's name
        $out[$canon] = ConvertTo-NarrationSchemaValue -Value $Parameters[$key] -Schema $schema -Backend $Backend
    }
    $out  # the schema-named, converted values, ready to be sent as JSON
}

# Builds a RuntimeDefinedParameter from a spec so Invoke-NarrationStage can offer a stage's options as real
# parameters (-Workers, -Takes, -Retakes) once -Name is known. -Spec comes from Get-NarrationParameterSpec.
# Returns the RuntimeDefinedParameter, carrying help text, aliases, ValidateSet and ValidateRange attributes.
function New-NarrationRuntimeParameter {
    # A RuntimeDefinedParameter for a spec (Invoke-NarrationStage's stage options).
    # PSScriptAnalyzer asks for -WhatIf on New-* functions; this one only builds an in-memory object, so it is exempt
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '',
        Justification = 'Builds an in-memory object only')]  # the object is only built, never saved
    param($Spec)  # the option's spec, as Get-NarrationParameterSpec returns it
    # the attribute list a runtime parameter carries
    $attrs = New-Object 'System.Collections.ObjectModel.Collection[System.Attribute]'
    # the attribute that makes it a real parameter and carries its Mandatory flag and help
    $pa = New-Object System.Management.Automation.ParameterAttribute
    $pa.Mandatory = $false  # stage options are optional overrides, never required
    $pa.HelpMessage = Get-NarrationParameterHelp $Spec  # help text for the parameter
    $attrs.Add($pa)  # every runtime parameter needs this attribute in its list
    # alternative names, if any
    if ($Spec.Alias.Count) { $attrs.Add((New-Object System.Management.Automation.AliasAttribute($Spec.Alias))) }
    # enum strings get a ValidateSet, which also gives tab completion of the allowed values
    if ($Spec.Enum.Count -and ($Spec.TypeName -eq 'string' -or $Spec.TypeName -eq 'string[]')) {
        # only the schema's enum strings are accepted; anything else fails before Python runs
        $attrs.Add((New-Object System.Management.Automation.ValidateSetAttribute($Spec.Enum)))
    }
    # numeric options with at least one bound get a ValidateRange
    if (($Spec.TypeName -eq 'int' -or $Spec.TypeName -eq 'double') -and ($null -ne $Spec.Minimum -or $null -ne $Spec.Maximum)) {
        $min = $Spec.Minimum; $max = $Spec.Maximum  # the bounds from the schema
        if ($Spec.TypeName -eq 'int') {  # an integer option needs [int] bounds so ValidateRange compares like with like
            # a missing bound becomes the widest [int] value
            if ($null -eq $min) { $min = [int]::MinValue }; if ($null -eq $max) { $max = [int]::MaxValue }
            # both bounds as [int] so their types match
            $attrs.Add((New-Object System.Management.Automation.ValidateRangeAttribute([int]$min, [int]$max)))
        }
        else {  # a double option takes its bounds as [double] values
            # a missing bound becomes the widest [double] value
            if ($null -eq $min) { $min = [double]::MinValue }; if ($null -eq $max) { $max = [double]::MaxValue }
            # both bounds as [double]
            $attrs.Add((New-Object System.Management.Automation.ValidateRangeAttribute([double]$min, [double]$max)))
        }
    }
    # name, type and attributes make the parameter
    New-Object System.Management.Automation.RuntimeDefinedParameter($Spec.Name, $Spec.Type, $attrs)
}

# ------------------------------------------------------------------------------------- generated commands
# Makes text safe to place inside a generated <# ... #> help block: '#>' would end the block early and '<#' would
# open a nested one, so a space is put between the characters; carriage returns are removed.
# -Text is the text. Returns the cleaned, trimmed string ('' for empty input).
# It does not guard a line that starts with a period: a line such as ".wav or .mp3 ..." reads as an unknown help
# keyword and help drops the whole block. The speak tool's out parameter (".wav, .mp3 or .m4a ...") is safe only
# because the comma right after the word stops it looking like a keyword.
function ConvertTo-NarrationHelpText {
    param([string]$Text)  # the description or help sentence to embed in a generated help block
    if (-not $Text) { return '' }  # a null or empty description becomes an empty string, never $null
    # break the comment markers apart, normalise line ends to LF, trim the ends
    ($Text -replace '#>', '# >' -replace '<#', '< #' -replace "`r", '').Trim()
}

# Quotes text as a PowerShell single-quoted literal for generated source: no $ expansion happens inside, and
# embedded single quotes are doubled. -Text is the text. Returns the literal including its quotes.
function ConvertTo-NarrationSingleQuoted {
    param([string]$Text)  # the text to quote
    "'" + ($Text -replace "'", "''") + "'"  # wrap in quotes, doubling any quote inside
}

# Hand-written examples for the built-in tools, keyed by tool name. Item 0 is the command line and item 1 the
# explanation that Get-Help shows under it. Tools not listed (plug-ins) get a generated example.
$script:ToolExamples = @{
    'speak' = @(  # speak: narration, which needs the Linux voice runtime
        "Invoke-NarrationSpeak -Text 'Hello there. This is a test.' -Out .\hello.wav",
        'Narrates the text into hello.wav with per-word timings in hello.words.json (Linux voice runtime; in Auto mode on Windows the call runs in WSL).')
    'quick' = @(  # quick: one call from a text file to an up-to-date project
        "Invoke-NarrationQuick -InputPath .\MyText.md -Out .\projects\my-text -Targets script",
        'Creates (or reuses) a project folder and brings the script stage (and what it needs) up to date.')
}

# Returns the two-line example used in a generated command's help: the command line and a sentence explaining it.
# -Tool is the tool description, -Specs its parameter specs, -Command the command name. Known tools use the
# hand-written text above; others get the command with each mandatory parameter filled with a placeholder.
function Get-NarrationToolExample {
    param($Tool, $Specs, [string]$Command)  # the tool description, its parameter specs and the command name
    $known = $script:ToolExamples[[string]$Tool.name]  # a hand-written example, if this tool has one
    if ($known) { return $known }  # a built-in tool returns its hand-written example unchanged
    $parts = @($Command)  # build the command line, starting with its name
    foreach ($s in $Specs) {  # add a placeholder value for each parameter, in schema order
        if (-not $s.Mandatory) { continue }  # only mandatory parameters appear in the generated example
        $v = switch ($s.TypeName) {  # choose a placeholder value from the parameter's type
            'int' { '2' }  # a sample whole number
            'double' { '1.5' }  # a sample fractional number
            'switch' { $null }  # switches take no value
            'hashtable' { '@{}' }  # an object parameter gets an empty hashtable literal as its sample
            'string[]' { "'a', 'b'" }  # a sample list of two strings
            default {  # strings and anything else: an allowed value, a path or quoted text
                if ($s.Enum.Count) { $s.Enum[0] }  # the first allowed value when there is a list of them
                elseif ($s.IsPath) { ".\$($s.Key)" }  # a relative path named after the parameter
                else { "'text'" }  # sample text in quotes
            }
        }
        # a switch is written bare; other parameters as -Name value
        if ($s.Switch) { $parts += "-$($s.Name)" } else { $parts += "-$($s.Name) $v" }
    }
    @(($parts -join ' '), "Runs the '$($Tool.name)' tool.")  # the line, plus a generic explanation
}

# Generates the PowerShell source of one per-tool command (for example Invoke-NarrationSpeak): comment-based help,
# typed parameters with validation, pipeline binding for the first path parameter, and a process block that hands
# the values to Invoke-NarrationTool. -Tool is a tool description from `speechkit-voice-tts-offline-pipeline tools`. Returns a [pscustomobject]
# with Name, Tool, Source, Text (the source) and Specs. Nothing is executed here; Update-NarrationCommand loads the text.
function New-NarrationToolFunctionText {
    # PSScriptAnalyzer asks for -WhatIf on New-* functions; this one only builds source text in memory, so it is exempt
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '',
        Justification = 'Builds source text in memory only')]  # nothing is executed while the text is built
    param($Tool)  # one tool description (name, description, params, platforms, source, runs_here)
    $command = Get-NarrationToolCommandName -Tool ([string]$Tool.name)  # Invoke-Narration plus the PascalCase tool name
    $required = @()  # names of required parameters
    $props = $null  # the schema's properties object
    if ($Tool.params) {  # the tool declares parameters
        # the schema's properties object, one entry per tool parameter
        $props = Get-NarrationSchemaProperty $Tool.params 'properties'
        $required = @(Get-NarrationSchemaProperty $Tool.params 'required')  # and which ones are required
    }
    $specs = Get-NarrationParameterSpec -Properties $props -Required $required  # one spec per parameter
    $sb = New-Object System.Text.StringBuilder  # the source text is assembled here
    $nl = "`n"  # LF line ends; PowerShell accepts them on every platform
    [void]$sb.Append("function $command {$nl<#$nl.SYNOPSIS$nl")  # the function line and the start of its help block
    # the tool's description becomes the synopsis
    [void]$sb.Append((ConvertTo-NarrationHelpText ([string]$Tool.description)) + $nl)
    [void]$sb.Append(".DESCRIPTION$nl")  # start the .DESCRIPTION section of the help block
    # where the command came from: the speechkit-voice-tts-offline-pipeline tool name and its source (continued on the next line)
    $desc = "Generated by the SpeechKitVoiceTtsOfflinePipeline module from the speechkit-voice-tts-offline-pipeline tool '$($Tool.name)' (source: $($Tool.source)). " +
            # the python command it runs and the platforms the tool supports
            "Runs: python -m speechkit_narrate tool $($Tool.name). Platforms: $(@($Tool.platforms) -join ', ')."
    # warn when the tool cannot run natively here and depends on WSL delegation
    if (-not $Tool.runs_here) { $desc += ' This tool cannot run natively here; in Auto mode on Windows the toolkit delegates it to WSL.' }
    $desc += ' Regenerate with Update-NarrationCommand after adding plug-ins.'  # remind the reader how to regenerate
    [void]$sb.Append($desc + $nl)  # append the finished description paragraph and a line break
    foreach ($s in $specs) {  # one .PARAMETER section per parameter
        # its help sentence, made safe for the help block
        [void]$sb.Append(".PARAMETER $($s.Name)$nl$(ConvertTo-NarrationHelpText (Get-NarrationParameterHelp $s))$nl")
    }
    $ex = Get-NarrationToolExample -Tool $Tool -Specs $specs -Command $command  # the example command and its explanation
    # first example: the command itself
    [void]$sb.Append(".EXAMPLE$nl$($ex[0])$nl$nl$(ConvertTo-NarrationHelpText $ex[1])$nl")
    # second example: keep the result and open the files it lists
    [void]$sb.Append(".EXAMPLE$nl`$r = $($ex[0])$nl`$r.Files | Get-Item$nl$nl")
    # explanation of the second example
    [void]$sb.Append("Keeps the result; its Files property lists the files the tool wrote.$nl")
    # output type, related commands and the end of the help block
    [void]$sb.Append(".OUTPUTS${nl}SpeechKitVoiceTtsOfflinePipeline.ToolResult$nl.LINK${nl}Invoke-NarrationTool$nl.LINK${nl}Update-NarrationCommand$nl#>$nl")
    # advanced function attributes and the start of the param block
    [void]$sb.Append("    [CmdletBinding()]$nl    [OutputType('SpeechKitVoiceTtsOfflinePipeline.ToolResult')]$nl    param($nl")
    $position = 0  # mandatory parameters are also positional, in schema order
    $pipelineBound = $false  # only one parameter may take pipeline input by property name
    $paramText = @()  # source of each parameter
    foreach ($s in $specs) {  # build each parameter's source
        $attr = @()  # arguments for its [Parameter()] attribute
        # required parameters are mandatory and get the next position
        if ($s.Mandatory) { $attr += 'Mandatory = $true'; $attr += "Position = $position"; $position++ }
        $aliases = @($s.Alias)  # start from the spec's aliases, which exist only for renamed parameters
        if ($s.IsPath -and -not $pipelineBound -and $s.TypeName -eq 'string') {  # the first plain-string path parameter
            $attr += 'ValueFromPipelineByPropertyName = $true'  # lets piped objects bind by property name
            # FullName is the property Get-Item and Get-ChildItem objects carry, so piping a file just works
            $aliases += 'FullName'
            $pipelineBound = $true  # no other parameter takes pipeline input
        }
        $lines = @()  # the lines of this parameter
        # the [Parameter()] attribute, when it has arguments
        if ($attr.Count) { $lines += "        [Parameter($($attr -join ', '))]" }
        # the [Alias()] attribute with each name single-quoted
        if ($aliases.Count) { $lines += "        [Alias($(($aliases | ForEach-Object { ConvertTo-NarrationSingleQuoted $_ }) -join ', '))]" }
        # a text or text-list parameter whose schema lists its allowed values
        if ($s.Enum.Count -and ($s.TypeName -eq 'string' -or $s.TypeName -eq 'string[]')) {
            # become a ValidateSet attribute
            $lines += "        [ValidateSet($(($s.Enum | ForEach-Object { ConvertTo-NarrationSingleQuoted $_ }) -join ', '))]"
        }
        # an int or double parameter whose schema sets a minimum or a maximum
        if (($s.TypeName -eq 'int' -or $s.TypeName -eq 'double') -and ($null -ne $s.Minimum -or $null -ne $s.Maximum)) {
            $min = $s.Minimum; $max = $s.Maximum  # the bounds from the schema
            # missing lower bound becomes the type's minimum, written as source
            if ($null -eq $min) { $min = "[$($s.TypeName)]::MinValue" }
            # missing upper bound becomes the type's maximum
            if ($null -eq $max) { $max = "[$($s.TypeName)]::MaxValue" }
            # emit the range check, for example [ValidateRange(1, [int]::MaxValue)]
            $lines += "        [ValidateRange($min, $max)]"
        }
        $lines += "        [$($s.TypeName)]`$$($s.Name)"  # the type and the variable, such as [string]$Text
        $paramText += ($lines -join $nl)  # join this parameter's lines
    }
    # parameters separated by a comma and a blank line, then the param block closes
    [void]$sb.Append(($paramText -join ",$nl$nl") + "$nl    )$nl")
    # source of a hashtable mapping each PowerShell name to the schema's key
    $map = ($specs | ForEach-Object { "$(ConvertTo-NarrationSingleQuoted $_.Name) = $(ConvertTo-NarrationSingleQuoted $_.Key)" }) -join '; '
    $toolName = ConvertTo-NarrationSingleQuoted ([string]$Tool.name)  # the tool name as a quoted literal
    # Appends the process block of the generated command. It runs once per pipeline object and:
    # copies only the tool's own bound parameters into an ordered dictionary under their schema names, turning switches
    # into booleans; forwards -Verbose, -Debug and the other preference parameters so the log can be watched; then runs
    # Invoke-NarrationTool inside the SpeechKitVoiceTtsOfflinePipeline module's own scope (& $module { ... }), so it reaches the module instance
    # that generated it. A failure is rethrown with ThrowTerminatingError so it appears to come from the generated
    # command.
    # The $__bc prefix keeps these working variables from colliding with any parameter variable.
    [void]$sb.Append(@"
    process {
        `$__bcMap = @{ $map }
        `$__bcParams = [ordered]@{}
        foreach (`$__bcKey in @(`$PSBoundParameters.Keys)) {
            if (`$__bcMap.ContainsKey(`$__bcKey)) {
                `$__bcValue = `$PSBoundParameters[`$__bcKey]
                if (`$__bcValue -is [System.Management.Automation.SwitchParameter]) { `$__bcValue = `$__bcValue.IsPresent }
                `$__bcParams[`$__bcMap[`$__bcKey]] = `$__bcValue
            }
        }
        `$__bcCommon = @{}
        foreach (`$__bcName in @('Verbose', 'Debug', 'ErrorAction', 'WarningAction', 'InformationAction', 'ProgressAction')) {
            if (`$PSBoundParameters.ContainsKey(`$__bcName)) { `$__bcCommon[`$__bcName] = `$PSBoundParameters[`$__bcName] }
        }
        try {
            & `$script:NarrationOwner { param(`$n, `$p, `$c) Invoke-NarrationTool -Name `$n -Parameters `$p @c } $toolName `$__bcParams `$__bcCommon
        }
        catch {
            `$PSCmdlet.ThrowTerminatingError(`$_)
        }
    }
}

"@)  # end of the here-string; the function source is complete
    # the generated command and what it came from
    [pscustomobject]@{ Name = $command; Tool = [string]$Tool.name; Source = [string]$Tool.source; Text = $sb.ToString(); Specs = $specs }
}

# Removes the generated per-tool commands and the SpeechKitVoiceTtsOfflinePipeline.Tools module. Takes no parameters and returns nothing.
# Called before regenerating them and when the SpeechKitVoiceTtsOfflinePipeline module itself is removed.
function Remove-NarrationToolModule {
    # PSScriptAnalyzer asks for -WhatIf on Remove-* functions; this private helper removes only the commands and module
    # this module generated, so it is exempt
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '',
        Justification = 'Removes only the commands this module generated')]  # only generated commands are removed
    param()  # takes no parameters; the empty block is where the attribute above is attached
    # Removes the generated commands. They were imported globally, so each one is removed through the Function:
    # drive (Remove-Module called from inside this module would only clear this module's own view of them);
    # only functions that belong to SpeechKitVoiceTtsOfflinePipeline.Tools are touched.
    # the module this session generated plus any other loaded copy, without nulls or duplicates
    $mods = @(@($script:ToolsModule) + @(Get-Module -Name 'SpeechKitVoiceTtsOfflinePipeline.Tools' -All) | Where-Object { $_ } | Select-Object -Unique)
    foreach ($m in $mods) {  # each copy of the module
        foreach ($n in @($m.ExportedFunctions.Keys)) {  # every command it exported
            # the global function of that name, if there is one
            $f = Get-Item -LiteralPath "Function:\$n" -ErrorAction SilentlyContinue
            # remove it only when it really belongs to SpeechKitVoiceTtsOfflinePipeline.Tools, so a user's own function of the same name
            # survives
            if ($f -and $f.ModuleName -eq 'SpeechKitVoiceTtsOfflinePipeline.Tools') { Remove-Item -LiteralPath "Function:\$n" -Force -ErrorAction SilentlyContinue }
        }
        Remove-Module -ModuleInfo $m -Force -ErrorAction SilentlyContinue  # then unload the module object itself
    }
    $script:ToolsModule = $null  # nothing is tracked any more
}

# Generates the source of the whole SpeechKitVoiceTtsOfflinePipeline.Tools dynamic module: a param block receiving the SpeechKitVoiceTtsOfflinePipeline module,
# one function per tool, and an Export-ModuleMember line. -Tools is the array from `speechkit-voice-tts-offline-pipeline tools`.
# Returns a [pscustomobject] with Text (the source) and Commands (the per-tool results).
function New-NarrationToolModuleText {
    # PSScriptAnalyzer asks for -WhatIf on New-* functions; this one only builds source text in memory, so it is exempt
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '',
        Justification = 'Builds source text in memory only')]  # the module text is only assembled here
    param([object[]]$Tools)  # the parsed tool list, built-in and plug-in tools alike
    $defs = @(foreach ($t in $Tools) { New-NarrationToolFunctionText -Tool $t })  # the source of each command
    # the command names as quoted literals
    $names = @($defs | ForEach-Object { ConvertTo-NarrationSingleQuoted $_.Name })
    # the module receives the SpeechKitVoiceTtsOfflinePipeline module object (New-Module -ArgumentList) and keeps it in $script:NarrationOwner
    # for the generated commands
    $text = "param(`$NarrationOwner)`n`$script:NarrationOwner = `$NarrationOwner`n`n" +
            (($defs | ForEach-Object { $_.Text }) -join "`n")  # followed by every function's source
    # export the generated commands
    if ($names.Count) { $text += "Export-ModuleMember -Function $($names -join ', ')`n" }
    else { $text += "Export-ModuleMember -Function @()`n" }  # no tools: export nothing, explicitly
    [pscustomobject]@{ Text = $text; Commands = $defs }  # the source and the command list
}
