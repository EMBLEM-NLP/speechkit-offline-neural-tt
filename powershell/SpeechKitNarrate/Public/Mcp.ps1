# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
# Private helper: the default location of Claude Desktop's config file on this platform. Takes no parameters and
# returns the path string (the file may not exist yet). Used by Register-NarrationMcpServer when -ConfigPath is omitted.
function Get-NarrationDefaultMcpConfigPath {
    if ($script:OnWindows) {  # Windows: Claude Desktop keeps its config in the roaming AppData profile folder
        $base = $env:APPDATA  # %APPDATA%, the roaming profile folder
        # rebuild it from the home folder if the variable is missing
        if (-not $base) { $base = Join-Path $HOME 'AppData\Roaming' }
        # %APPDATA%\Claude\claude_desktop_config.json; nested Join-Path for 5.1, which joins only two parts
        return (Join-Path (Join-Path $base 'Claude') 'claude_desktop_config.json')
    }
    if ($PSVersionTable.PSVersion.Major -ge 6 -and $IsMacOS) {  # macOS; $IsMacOS only exists in PowerShell 6 and later
        # the macOS location under Application Support
        return (Join-Path $HOME 'Library/Application Support/Claude/claude_desktop_config.json')
    }
    $cfg = $env:XDG_CONFIG_HOME  # Linux follows the XDG base directory convention
    if (-not $cfg) { $cfg = Join-Path $HOME '.config' }  # its default when XDG_CONFIG_HOME is unset
    # Linux: normally ~/.config/Claude/claude_desktop_config.json, returned as the function's output
    Join-Path (Join-Path $cfg 'Claude') 'claude_desktop_config.json'
}

# Public command: runs the speechkit-narrate MCP server (`python -m speechkit_narrate --json mcp`) attached to this console's stdin and
# stdout, which carry the Model Context Protocol messages, until its input closes. Takes no parameters of its own
# (-WhatIf shows the command line instead of starting the server) and returns nothing; a non-zero exit writes an
# error. For MCP clients that start PowerShell themselves.
function Start-NarrationMcpServer {
    <#
    .SYNOPSIS
    Runs the speechkit-narrate MCP server (python -m speechkit_narrate mcp) on this console's stdin/stdout.

    .DESCRIPTION
    The server speaks the Model Context Protocol over stdio: its tools are generated from the stage and tool
    registry. It runs until its input closes. Use this when an MCP client starts PowerShell itself; to let
    Claude Desktop start the server directly, use Register-NarrationMcpServer instead.

    .EXAMPLE
    pwsh -NoProfile -Command "Import-Module C:\src\speechkit-offline-neural-tt\powershell\SpeechKitNarrate; Start-NarrationMcpServer"

    The command line an MCP client can use to start the server through PowerShell.

    .EXAMPLE
    Set-NarrationBackend Wsl -Distribution Ubuntu-24.04; Start-NarrationMcpServer

    Runs the whole server inside WSL.

    .EXAMPLE
    Start-NarrationMcpServer -WhatIf

    Prints the exact command line the server would be started with, and starts nothing.
    #>
    [CmdletBinding(SupportsShouldProcess = $true)]  # -WhatIf and -Confirm, so the command line can be checked first
    param()  # no parameters of its own
    try {  # any failure becomes this command's terminating error
        $spec = Get-NarrationLaunchSpec -Arguments @('mcp')  # how to start the server for the current backend
        $line = "$($spec.FilePath) $(ConvertTo-NarrationArgumentString $spec.ArgumentList)"  # the exact command line
        # with -Verbose, show the exact command (note that the host may print this on the same stdout the client reads)
        Write-Verbose "speechkit-narrate: $line"
        # -WhatIf prints the command line and stops here; a declined -Confirm stops here too
        if (-not $PSCmdlet.ShouldProcess('the speechkit-narrate MCP server', $line)) { return }
        # runs in the foreground, sharing this console's streams, and returns the exit code when the client disconnects
        $code = Invoke-NarrationNative -FilePath $spec.FilePath -ArgumentList $spec.ArgumentList -Environment $spec.Environment
        if ($code -ne 0) {  # a non-zero exit means the server failed
            # non-terminating error with the code
            Write-Error -Message "speechkit-narrate mcp exited with code $code" -Category InvalidResult
        }
    }
    catch { $PSCmdlet.ThrowTerminatingError($_) }  # rethrow as this command's own error
}

# Public command: adds (or replaces) the speechkit-narrate entry in an MCP client's JSON config, Claude Desktop's by default,
# so the client starts the server itself. -ConfigPath picks the file, -Name the server's key in mcpServers, -Delegate
# overrides SPEECHKIT_DELEGATE ('' removes it), -NoBackup skips the timestamped copy. Every other setting in the file
# is kept. Supports -WhatIf. Writes the config (and a .bak copy); returns a SpeechKitNarrate.McpRegistration object.
function Register-NarrationMcpServer {
    <#
    .SYNOPSIS
    Adds the speechkit-narrate MCP server to an MCP client's config file (Claude Desktop by default).

    .DESCRIPTION
    Reads the config file (if it exists), backs it up next to itself, and merges an "mcpServers" entry:
      command  the Python that runs the toolkit (or wsl.exe with the Wsl backend)
      args     -m speechkit_narrate --json mcp
      env      PYTHONPATH (the toolkit; left out in Installed mode, where the package is installed),
               PYTHONUTF8=1, and SPEECHKIT_DELEGATE when Linux-only stages go to WSL
    Every other entry and setting in the file is kept. Restart the client to pick the server up.

    In Installed mode (no toolkit folder) the command is the chosen Python, and nothing is written when that Python
    cannot import the speechkit_narrate package, because the client would only show a server that fails to start.

    .PARAMETER ConfigPath
    The client's config file. Default: $env:APPDATA\Claude\claude_desktop_config.json on Windows
    (~/Library/Application Support/Claude/... on macOS, ~/.config/Claude/... on Linux).

    .PARAMETER Name
    The server's name in mcpServers (default speechkit-narrate).

    .PARAMETER Delegate
    SPEECHKIT_DELEGATE to write (default: what the Auto backend uses on Windows, e.g. "wsl.exe -d Ubuntu-24.04 --exec").

    .PARAMETER NoBackup
    Do not copy the existing file to <file>.<timestamp>.bak first.

    .EXAMPLE
    Register-NarrationMcpServer

    Adds speechkit-narrate to Claude Desktop's servers; restart Claude Desktop afterwards.

    .EXAMPLE
    Register-NarrationMcpServer -ConfigPath .\mcp.json -Name narrate-dev -WhatIf

    Shows the entry it would write.

    .OUTPUTS
    SpeechKitNarrate.McpRegistration
    #>
    # SupportsShouldProcess adds -WhatIf and -Confirm; Medium impact asks only when -Confirm is given
    [CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
    [OutputType('SpeechKitNarrate.McpRegistration')]  # declares the result type for Get-Help
    param(  # four optional parameters: config file, server name, delegate override and the no-backup switch
        [string]$ConfigPath,  # config file; empty means Claude Desktop's default location

        [ValidateNotNullOrEmpty()]  # the server name must not be empty
        [string]$Name = 'speechkit-narrate',  # the key under mcpServers

        [string]$Delegate,  # explicit SPEECHKIT_DELEGATE value; '' turns delegation off in the entry

        [switch]$NoBackup  # skip the backup copy
    )
    try {  # errors below become this command's terminating error
        # Installed mode with a native Python: the client starts that Python itself, so it must be able to import the
        # package now (Source mode does not need this, because PYTHONPATH in the entry finds the toolkit folder).
        $clientRunsPython = (Get-NarrationMode) -eq 'Installed' -and (Get-NarrationEffectiveBackend) -eq 'Native'
        if ($clientRunsPython -and -not (Test-NarrationPackage)) {  # and that Python cannot import speechkit_narrate yet
            # say that nothing was written, then what to do (the hint begins with a space, which is trimmed)
            $PSCmdlet.ThrowTerminatingError((New-NarrationErrorRecord -Type 'ModuleNotFoundError' -Target 'speechkit_narrate' `
                        -Message ('Nothing was written to the MCP client config. ' +  # says that nothing was written
                        # then what to do about it, with the hint's leading space trimmed
                        (Get-NarrationMissingPackageHint -Text "No module named 'speechkit_narrate'").TrimStart())))
        }
        if (-not $ConfigPath) { $ConfigPath = Get-NarrationDefaultMcpConfigPath }  # fall back to Claude Desktop's file
        $file = Resolve-NarrationPath $ConfigPath  # absolute path, relative to the current location
        # the stable launch description: no inherited PYTHONPATH, plug-ins and delegate python spelled out
        $spec = Get-NarrationLaunchSpec -Arguments @('mcp') -ForRegistration
        $envMap = [ordered]@{}  # the environment block of the entry
        foreach ($k in @($spec.Environment.Keys)) {  # copy the launch variables
            # PYTHONUTF8 already makes Python's pipes UTF-8, and WSL_UTF8 only affects wsl.exe's own messages
            if (@('PYTHONIOENCODING', 'WSL_UTF8') -contains $k) { continue }
            $envMap[$k] = [string]$spec.Environment[$k]  # everything else is written as a string
        }
        if ($PSBoundParameters.ContainsKey('Delegate')) {  # the caller chose the delegate explicitly
            # a value replaces the computed one; an empty value removes delegation from the entry
            if ($Delegate) { $envMap['SPEECHKIT_DELEGATE'] = $Delegate } else { $envMap.Remove('SPEECHKIT_DELEGATE') }
        }
        $entry = [ordered]@{  # the mcpServers entry: what Claude Desktop starts
            command = $spec.FilePath  # Python, or wsl.exe for the Wsl backend
            # [object[]] so the JSON has an array even when there is a single argument
            args    = [object[]]@($spec.ArgumentList)
            env     = $envMap  # environment variables for the server
        }

        $config = [ordered]@{}  # the whole config, built up below
        $exists = Test-Path -LiteralPath $file -PathType Leaf  # remember whether there is a file to keep
        if ($exists) {  # an existing file must be merged, not overwritten
            $text = [System.IO.File]::ReadAllText($file)  # read it whole; .NET detects a byte order mark if present
            if ($text.Trim()) {  # an empty file is treated like a missing one
                # read the existing config into objects; on PowerShell 7 nesting up to depth 100 is kept
                try { $parsed = ConvertFrom-NarrationJson -Json $text }
                catch {  # the file is not valid JSON
                    # stop without touching the file, naming it and the parser's complaint
                    $PSCmdlet.ThrowTerminatingError((New-NarrationErrorRecord -Message "$file is not valid JSON; fix it or move it away first: $($_.Exception.Message)" -Type 'ValueError' -Target $file))
                }
                # plain ordered dictionaries can be extended and keep the file's key order
                $config = ConvertTo-NarrationPlain $parsed
                if ($config -isnot [System.Collections.IDictionary]) {  # the top level must be a JSON object
                    # refuse to replace an array or a bare value
                    $PSCmdlet.ThrowTerminatingError((New-NarrationErrorRecord -Message "$file does not hold a JSON object." -Type 'ValueError' -Target $file))
                }
            }
        }
        # no mcpServers section yet, or one of the wrong type
        if (-not $config.Contains('mcpServers') -or $config['mcpServers'] -isnot [System.Collections.IDictionary]) {
            $config['mcpServers'] = [ordered]@{}  # start an empty section
        }
        $config['mcpServers'][$Name] = $entry  # add or replace only this server; other servers stay as they were
        # readable, indented JSON; depth 32 keeps nested settings intact
        $json = ConvertTo-Json -InputObject $config -Depth 32
        $backup = $null  # path of the backup, if one is made
        # -WhatIf stops here after describing the command it would register
        if (-not $PSCmdlet.ShouldProcess($file, "Register MCP server '$Name': $($spec.FilePath) $(ConvertTo-NarrationArgumentString $spec.ArgumentList)")) { return }
        $dir = Split-Path -Parent $file  # the folder that holds the config
        # create it if Claude Desktop has never run
        if ($dir -and -not (Test-Path -LiteralPath $dir)) { [void](New-Item -ItemType Directory -Path $dir -Force) }
        if ($exists -and -not $NoBackup) {  # back up an existing file unless told not to
            # name it <file>.<yyyyMMdd-HHmmss>.bak, e.g. claude_desktop_config.json.20240501-134500.bak
            $backup = "$file.$(Get-Date -Format 'yyyyMMdd-HHmmss').bak"
            Copy-Item -LiteralPath $file -Destination $backup -Force  # copy the current file to the backup
        }
        # Write next to the file first, so a failure cannot leave a half-written config behind.
        $tmp = "$file.tmp"  # temporary file next to the target
        try {  # the finally block removes the temporary file whether or not the copy worked
            # write the new JSON as UTF-8 without a BOM, ending with a newline
            [System.IO.File]::WriteAllText($tmp, $json + [Environment]::NewLine, $script:Utf8)
            # copy it over the real file (overwrite allowed); a plain copy, so it is not an atomic swap
            [System.IO.File]::Copy($tmp, $file, $true)
        }
        finally {  # also after a failed write or copy, so no stray <file>.tmp is left next to the config
            if (Test-Path -LiteralPath $tmp -PathType Leaf) { [System.IO.File]::Delete($tmp) }
        }
        New-NarrationObject -TypeName 'SpeechKitNarrate.McpRegistration' -Property ([ordered]@{  # describe what was registered
                Name       = $Name  # Name: the key written under mcpServers, speechkit-narrate unless -Name was given
                ConfigPath = $file  # the file that was changed
                Backup     = $backup  # backup file, or $null
                Command    = $entry.command  # program the client will start
                # Args: the command-line arguments as a string array, such as -m speechkit_narrate --json mcp
                Args       = [string[]]$entry.args
                Env        = $envMap  # Env: the variables written into the entry, such as PYTHONPATH and PYTHONUTF8
            })
    }
    catch { $PSCmdlet.ThrowTerminatingError($_) }  # rethrow as this command's terminating error
}
