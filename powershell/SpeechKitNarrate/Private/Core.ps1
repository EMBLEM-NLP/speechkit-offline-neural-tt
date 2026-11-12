# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
# Module configuration, platform facts, Python discovery and the process helpers.
# Everything here is module-scoped ($script:); nothing is written to the global scope.
#
# Source mode and Installed mode: where the speechkit_narrate Python package comes from.
#   Source     the module sits in the speechkit-offline-neural-tt checkout (or Set-NarrationBackend -ToolkitRoot /
#              $env:SPEECHKIT_ROOT names one). Python finds the package through PYTHONPATH.
#   Installed  no checkout is configured. The Python that runs the toolkit must have the speechkit-narrate package
#              installed (pip install <checkout>); nothing is put on PYTHONPATH.

# Sets up the module-wide state once, when SpeechKitNarrate.psm1 is imported. Takes no parameters and returns nothing.
# It records platform facts ($script:OnWindows, $script:HasArgumentList, $script:Utf8), builds $script:Config from
# the SPEECHKIT_BACKEND, SPEECHKIT_WSL_DISTRO and SPEECHKIT_ROOT environment variables, and creates empty caches.
# Set-NarrationBackend later edits the same $script:Config for the rest of the session.
function Initialize-NarrationConfig {
    # True on any NT-based Windows. Works on Windows PowerShell 5.1, where the automatic $IsWindows variable does not
    # exist.
    $script:OnWindows = ([System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT)
    # ProcessStartInfo.ArgumentList exists on .NET Core 2.1 and later (PowerShell 7) but not on .NET Framework (5.1).
    # When it exists, each argument is handed to the child untouched; otherwise one quoted string has to be built.
    $script:HasArgumentList = ($null -ne [System.Diagnostics.ProcessStartInfo].GetProperty('ArgumentList'))
    # UTF-8 without a byte order mark: used to decode the child's stdout/stderr and to write JSON files, because a
    # BOM at the start of a JSON file or of Python's output would break parsing.
    $script:Utf8 = New-Object System.Text.UTF8Encoding($false)

    $backend = 'Auto'  # the default: native Python, Linux-only stages delegated to WSL on Windows
    # Accept $env:SPEECHKIT_BACKEND only when it names a real backend; -contains compares case-insensitively.
    # Any other value is ignored and the default stays.
    if ($env:SPEECHKIT_BACKEND -and @('Auto', 'Native', 'Wsl') -contains $env:SPEECHKIT_BACKEND) {
        # normalise the casing ('wsl' or 'WSL' becomes 'Wsl') so later -eq and ValidateSet comparisons read cleanly
        $backend = (Get-Culture).TextInfo.ToTitleCase($env:SPEECHKIT_BACKEND.ToLowerInvariant())
    }
    # an explicit toolkit location, needed only when the module folder was moved out of the checkout
    $root = $env:SPEECHKIT_ROOT
    if (-not $root) {  # no override, so derive the location from where the module lives
        # <checkout>\powershell\SpeechKitNarrate -> <checkout>: two Split-Path -Parent calls climb to the checkout
        $guess = Split-Path -Parent (Split-Path -Parent $script:ModuleRoot)
        # The module is part of a checkout when that folder holds the speechkit_narrate package. Otherwise it was
        # copied somewhere on its own: the root stays empty and Installed mode expects a pip-installed package.
        if (Test-NarrationToolkitFolder $guess) { $root = $guess }
    }
    # The session's settings. Every launcher, path translation and status command reads from here.
    $script:Config = @{
        Backend        = $backend  # Auto, Native or Wsl
        # [string] turns an unset variable into '', which means "WSL's default distribution"
        Distribution   = [string]$env:SPEECHKIT_WSL_DISTRO
        WslPython      = ''  # python inside WSL; '' means probe for /opt/speechkit-narrate/venv, else use python3
        Python         = ''  # native Python command or path; '' means use $env:SPEECHKIT_PYTHON or discovery
        ToolkitRoot    = $root  # the checkout that contains the speechkit_narrate package; $null in Installed mode
        WslEnvironment = @{}  # extra NAME=VALUE pairs passed through `env` to commands run inside WSL
    }
    Reset-NarrationCache  # start with empty caches for this configuration
}

# Tells whether a folder is a toolkit root, i.e. holds speechkit_narrate\__main__.py. -Path is the folder (may be empty).
# Returns $true or $false and never throws.
function Test-NarrationToolkitFolder {
    param([string]$Path)  # the folder to check
    if (-not $Path) { return $false }  # nothing to check
    # nested Join-Path keeps 5.1 compatibility (it takes only two parts there)
    Test-Path -LiteralPath (Join-Path (Join-Path $Path 'speechkit_narrate') '__main__.py') -PathType Leaf
}

# Creates (or replaces) $script:Cache, the store of results that cost a process launch to compute. Takes no
# parameters and returns nothing. Called on import and whenever Set-NarrationBackend changes anything, because a
# different backend, Python or distribution makes every cached answer stale.
function Reset-NarrationCache {
    # PSScriptAnalyzer asks for -WhatIf on Reset-* functions; this one only replaces an in-memory cache, so it is exempt
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '',
        Justification = 'Replaces an in-memory cache only')]  # the cache swap needs no confirmation
    param()  # takes no parameters; the empty block is where the attribute above is attached
    $script:Cache = @{  # one hashtable keyed by what is cached
        Python     = $null  # the resolved native Python (Executable, Version, Command), or $null before the first probe
        WslPython  = $null  # the python path inside WSL once probed (venv python or python3)
        WslPath    = @{}  # Windows path to WSL path answers from wslpath, keyed by launcher, distribution and path
        WslReverse = @{}  # WSL path to Windows path, learned from each translation and used to map results back
        Registry   = @{}  # `stages` results with the time they were fetched, keyed by backend, project and plug-ins
        Tools      = @{}  # `tools` results keyed by backend and plug-in set
    }
}

# Returns the toolkit folder (a string path) after checking that it really holds the speechkit_narrate package, or
# $null in Installed mode, where there is no toolkit folder and Python imports the installed package.
# Throws a message naming Set-NarrationBackend -ToolkitRoot when a configured folder lacks the package, so a moved
# module fails with advice instead of a Python "No module named speechkit_narrate" error. Used by the launcher in
# Invoke.ps1.
function Get-NarrationToolkitRoot {
    $root = $script:Config.ToolkitRoot  # the folder chosen at import or by Set-NarrationBackend -ToolkitRoot
    if (-not $root) { return $null }  # Installed mode: nothing to check, and nothing to put on PYTHONPATH
    if (-not (Test-NarrationToolkitFolder $root)) {  # python -m speechkit_narrate needs <root>\speechkit_narrate\__main__.py
        # stop with a message that says where it looked and how to fix it (continued on the next line)
        throw "The speechkit-narrate toolkit was not found at '$root' (expected a 'speechkit_narrate' package folder there). " +
              "Point the module at the checkout with: Set-NarrationBackend -ToolkitRoot 'C:\path\to\speechkit-offline-neural-tt'"
    }
    $root  # return the verified folder
}

# Tells which of the two modes this session is in. Takes no parameters. Returns 'Source' when a toolkit folder is
# configured and 'Installed' when it is not.
function Get-NarrationMode {
    if ($script:Config.ToolkitRoot) { 'Source' } else { 'Installed' }  # a configured folder means Source mode
}

# Builds the advice shown when Python reports that the speechkit_narrate package cannot be imported. -Text is what
# Python printed. Returns the advice as a string, or '' when Text is not that error or the module is in Source mode
# (where the usual cause is a wrong toolkit folder, which Get-NarrationToolkitRoot already explains).
function Get-NarrationMissingPackageHint {
    param([string]$Text)  # Python's error text
    if ((Get-NarrationMode) -ne 'Installed') { return '' }  # Source mode has its own message
    if ($Text -notmatch "No module named '?speechkit_narrate'?") { return '' }  # some other failure
    # the two fixes: install the package into this Python, or point the module at a checkout
    ' The speechkit-narrate Python package is not installed for this Python. Install it with ' +
        "python -m pip install 'C:\path\to\speechkit-offline-neural-tt', or point the module at the checkout: " +
        "Set-NarrationBackend -ToolkitRoot 'C:\path\to\speechkit-offline-neural-tt'"
}

# Tells whether the Python this module would run can import the speechkit_narrate package. Takes no parameters.
# Returns $true or $false; $false also when no Python is found or it does not answer within a minute. Starts Python
# once. Used before writing an MCP client's config in Installed mode, where a server that cannot import the package
# would fail silently inside the client.
function Test-NarrationPackage {
    try { $py = Resolve-NarrationPython } catch { return $false }  # no usable Python at all
    try {  # a probe must never throw into its caller
        # `import speechkit_narrate` succeeds only when the package is installed for this Python (or on its path)
        $r = Invoke-NarrationProcess -FilePath $py.Executable -ArgumentList @('-c', 'import speechkit_narrate') -TimeoutSeconds 60
        $r.ExitCode -eq 0  # zero: the import worked
    }
    catch { $false }  # the interpreter could not be started or hung
}

# Collapses the configured backend to how Python is actually launched: 'Wsl' (inside WSL) or 'Native'.
# Auto counts as Native because it starts Python on this machine; only the toolkit itself sends some stages to WSL.
# -Backend (string, optional) overrides the session setting. Returns the string 'Wsl' or 'Native'.
function Get-NarrationEffectiveBackend {
    param([string]$Backend)  # an explicit backend name, or empty for the session's setting
    if (-not $Backend) { $Backend = $script:Config.Backend }  # fall back to the configured backend
    # only Wsl changes how the process is started and how paths are written
    if ($Backend -eq 'Wsl') { 'Wsl' } else { 'Native' }
}

# Splits a command line held in one string, such as $env:SPEECHKIT_PYTHON ('py -3.13') or $env:SPEECHKIT_WSL, into
# an array of tokens. Single or double quotes group words containing spaces and are removed; backslashes are kept
# literally, so Windows paths survive. Returns a string array (possibly empty). No side effects.
function Split-NarrationCommandLine {
    # Splits "python3 'C:\My Tools\fake_wsl.py'" into tokens; quotes group, backslashes are literal.
    param([string]$CommandLine)
    # a growable string list that collects each completed token in order
    $tokens = New-Object System.Collections.Generic.List[string]
    $sb = New-Object System.Text.StringBuilder  # characters of the token being collected
    $quote = [char]0  # the quote character currently open, or the NUL char when outside quotes
    $started = $false  # remembers that a quote opened a token, so '' still yields an empty token
    foreach ($ch in $CommandLine.ToCharArray()) {  # walk the text one character at a time
        if ($quote -ne [char]0) {  # inside a quoted section
            # the matching quote closes the section; anything else (spaces included) is part of the token
            if ($ch -eq $quote) { $quote = [char]0 } else { [void]$sb.Append($ch) }
            continue  # nothing else applies inside quotes
        }
        # an opening quote starts a quoted section and marks the token as started
        if ($ch -eq [char]'"' -or $ch -eq [char]"'") { $quote = $ch; $started = $true; continue }
        if ([char]::IsWhiteSpace($ch)) {  # whitespace outside quotes ends the current token
            # emit the token if it has text or was quoted, then reset for the next one
            if ($sb.Length -gt 0 -or $started) { $tokens.Add($sb.ToString()); [void]$sb.Clear(); $started = $false }
            continue  # runs of spaces produce no empty tokens
        }
        [void]$sb.Append($ch)  # an ordinary character joins the current token
    }
    # flush the last token, which has no trailing space
    if ($sb.Length -gt 0 -or $started) { $tokens.Add($sb.ToString()) }
    # the leading comma returns the array as one object so PowerShell does not unroll a one-element result into a bare
    # string
    , $tokens.ToArray()
}

# Joins an argument array into one command-line string that a Windows program's C runtime (CommandLineToArgvW)
# splits back into exactly the same arguments. -ArgumentList is a string array. Returns a single string.
# Needed on Windows PowerShell 5.1, where ProcessStartInfo only takes a string, and for showing commands in
# verbose output, -WhatIf text and SPEECHKIT_DELEGATE.
function ConvertTo-NarrationArgumentString {
    # Quotes arguments the way CommandLineToArgvW / the MS C runtime parse them back.
    # Used where ProcessStartInfo.ArgumentList does not exist (Windows PowerShell 5.1) and for display.
    param([string[]]$ArgumentList)
    # the foreach statement's output (one quoted piece per argument) is collected into $parts
    $parts = foreach ($a in $ArgumentList) {
        # an empty argument must still occupy a position, so it is written as a pair of quotes
        if ($null -eq $a -or $a -eq '') { '""'; continue }
        if ($a -notmatch '[\s"]') { $a; continue }  # plain words with no whitespace or quote need no quoting
        $sb = New-Object System.Text.StringBuilder  # builds the quoted form of this argument
        # start the wrapped argument with a double quote; [void] discards the builder that Append returns
        [void]$sb.Append('"')
        $slashes = 0  # number of backslashes seen in a row; their meaning depends on what follows them
        foreach ($ch in $a.ToCharArray()) {  # scan the argument character by character
            # hold back backslashes until we know whether a quote follows
            if ($ch -eq [char]'\') { $slashes++; continue }
            if ($ch -eq [char]'"') {  # a literal quote inside the argument
                # backslashes before a quote are doubled, plus one more to escape the quote itself
                [void]$sb.Append([char]'\', 2 * $slashes + 1)
                [void]$sb.Append([char]'"')  # then the quote, now escaped
            }
            else {  # not a backslash or a quote: flush any held backslashes, then copy the character
                # backslashes not followed by a quote are literal and go out unchanged
                if ($slashes) { [void]$sb.Append([char]'\', $slashes) }
                [void]$sb.Append($ch)  # copy the ordinary character unchanged after any held backslashes
            }
            $slashes = 0  # the run of backslashes has been written
        }
        # trailing backslashes are doubled so they do not escape the closing quote (matters for paths like C:\dir\)
        if ($slashes) { [void]$sb.Append([char]'\', 2 * $slashes) }
        [void]$sb.Append('"')  # end the wrapped argument with the matching double quote
        $sb.ToString()  # emit the quoted argument
    }
    $parts -join ' '  # one space between arguments
}

# Returns the current PowerShell folder as a real file-system path (string). Used as the working directory of every
# child process: Set-Location does not change the .NET process directory, so without this a relative path
# given to Python would be resolved against the wrong folder.
function Get-NarrationWorkingDirectory {
    # -PSProvider FileSystem gives the file-system location even when the user sits in HKLM: or Cert:, and ProviderPath
    # expands PowerShell-only drives to the real path
    (Get-Location -PSProvider FileSystem).ProviderPath
}

# Turns a PowerShell path (relative to the current location, ~ allowed, PS drives allowed) into an absolute
# file-system path string. Unlike Resolve-Path it works for files and folders that do not exist yet, such as a new
# project folder or an output prefix. -Path (string) is required. No side effects.
function Resolve-NarrationPath {
    # Absolute file-system path for a PowerShell path (relative to the current location, ~ allowed),
    # whether or not it exists yet.
    param([Parameter(Mandatory = $true)][string]$Path)
    # the engine's own resolver: relative paths use PowerShell's current location, not the process directory
    $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
}

# Builds a System.Diagnostics.ProcessStartInfo for a child process without starting it.
# -FilePath is the program, -ArgumentList its arguments, -Environment a dictionary of variables to set (a $null
# value removes the variable), -Redirect captures stdin/stdout/stderr as UTF-8 text. Returns the start info.
# Shared by Invoke-NarrationProcess, Invoke-NarrationNative and Invoke-NarrationCli.
function New-NarrationStartInfo {
    # PSScriptAnalyzer asks for -WhatIf on New-* functions; this one only builds a description and starts nothing
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '',
        Justification = 'Builds an in-memory description only')]  # nothing is started or changed here
    param(  # the start info settings: program, arguments, environment changes and the redirect switch
        [Parameter(Mandatory = $true)][string]$FilePath,  # program to run: a name on PATH or a full path
        [string[]]$ArgumentList = @(),  # arguments passed one by one, never through a shell
        # environment changes for the child only; this session's own environment is untouched
        [System.Collections.IDictionary]$Environment = @{},
        [switch]$Redirect  # capture the standard streams instead of sharing this console
    )
    $psi = New-Object System.Diagnostics.ProcessStartInfo  # a fresh, empty start description
    $psi.FileName = $FilePath  # the program to run, either a name looked up on PATH or a full path
    $psi.UseShellExecute = $false  # UseShellExecute must be off to redirect streams and to pass environment variables
    # run in PowerShell's current folder so relative paths agree with what the user typed
    $psi.WorkingDirectory = Get-NarrationWorkingDirectory
    if ($script:HasArgumentList) {  # PowerShell 7: hand arguments over as a list, so nothing is re-quoted or re-parsed
        # each argument is converted to string (numbers, paths) and added verbatim
        foreach ($a in $ArgumentList) { $psi.ArgumentList.Add([string]$a) }
    }
    else {  # Windows PowerShell 5.1 (.NET Framework) only accepts a single argument string
        # quote the arguments so the child's C runtime splits them back correctly
        $psi.Arguments = ConvertTo-NarrationArgumentString -ArgumentList $ArgumentList
    }
    # apply each requested environment change; @() copies the keys so the loop is safe
    foreach ($k in @($Environment.Keys)) {
        $v = $Environment[$k]  # the requested value for this variable
        # $null deletes the variable from the inherited environment
        if ($null -eq $v) { $psi.EnvironmentVariables.Remove([string]$k) }
        else { $psi.EnvironmentVariables[[string]$k] = [string]$v }  # otherwise set or overwrite it as text
    }
    if ($Redirect) {  # captured mode, used whenever the caller needs the output
        # stdin is redirected so it can be closed at once and a child cannot wait for keyboard input
        $psi.RedirectStandardInput = $true
        $psi.RedirectStandardOutput = $true  # collect stdout (Python prints its JSON result there)
        $psi.RedirectStandardError = $true  # collect stderr (Python's log lines go there)
        # decode as UTF-8, matching PYTHONUTF8 and PYTHONIOENCODING on the Python side
        $psi.StandardOutputEncoding = $script:Utf8
        $psi.StandardErrorEncoding = $script:Utf8  # same for the log stream so non-ASCII titles and names survive
    }
    $psi  # return the start info
}

# Starts a process from a ProcessStartInfo and returns the System.Diagnostics.Process object.
# A missing program normally surfaces as a vague .NET MethodInvocationException; this rethrows it as a short
# message that names the program, so callers can show something readable.
function Start-NarrationProcess {
    # PSScriptAnalyzer asks for -WhatIf on Start-* functions; this private helper is only reached from commands that
    # decide what runs (Install-NarrationDependency asks first), so it is exempt
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '',
        Justification = 'Private helper; the public callers decide and confirm')]  # callers ask before this runs
    param([System.Diagnostics.ProcessStartInfo]$StartInfo)  # the start description built by New-NarrationStartInfo
    try {  # catch a failed launch so the error can be reworded for the user
        [System.Diagnostics.Process]::Start($StartInfo)  # returns the running process
    }
    catch {  # launch failed, typically because the program does not exist
        # name the program; the inner Win32 message ("file not found") and the outer message are joined with no
        # separator between them
        throw "Could not start '$($StartInfo.FileName)': $($_.Exception.InnerException.Message)$($_.Exception.Message)"
    }
}

# Ends a child process if it is still running; used in finally blocks so Ctrl+C, a timeout or an exception never
# leaves Python or wsl.exe orphaned. -Process may be $null. Returns nothing and never throws.
function Stop-NarrationProcess {
    # PSScriptAnalyzer asks for -WhatIf on Stop-* functions; this private helper ends only a process this module started
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '',
        Justification = 'Private helper; ends only a child process the module started')]  # stops only its own child
    param([System.Diagnostics.Process]$Process)  # the process to stop
    if ($null -eq $Process) { return }  # nothing was started, nothing to do
    try {  # any error below means the process is already gone, so it is ignored
        if (-not $Process.HasExited) {  # a process that already exited needs no kill
            # Kill($true) also ends the process tree (PowerShell 7); .NET Framework lacks that overload, so 5.1 falls
            # back to Kill()
            try { $Process.Kill($true) } catch { $Process.Kill() }
        }
    }
    # swallowed on purpose: the process may exit between the check and the kill (the reason is only visible with
    # -Verbose)
    catch { Write-Verbose "speechkit-narrate: the process was already gone: $($_.Exception.Message)" }
}

# Runs a program to completion and returns its exit code and full output as a [pscustomobject] with ExitCode,
# StdOut and StdErr (strings). -FilePath, -ArgumentList and -Environment as for New-NarrationStartInfo;
# -TimeoutSeconds (seconds, 0 = wait forever) kills the program and throws when it runs too long.
# Used for short probes: the Python version check, wslpath, `wsl -l -q` and the WSL venv test.
function Invoke-NarrationProcess {
    # Runs a program to completion and captures its output (no live streaming).
    param(
        # required: the program to start, by name on PATH or by full path
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @(),  # arguments handed over one by one; none by default
        [System.Collections.IDictionary]$Environment = @{},  # environment changes for the child
        [int]$TimeoutSeconds = 0  # maximum run time in seconds; 0 disables the limit
    )
    # captured mode, so the output can be returned
    $psi = New-NarrationStartInfo -FilePath $FilePath -ArgumentList $ArgumentList -Environment $Environment -Redirect
    $p = Start-NarrationProcess -StartInfo $psi  # launch (throws a readable message if the program is missing)
    try {  # the finally block below always cleans up
        # closing stdin gives the child end-of-file at once instead of letting it wait for input
        $p.StandardInput.Close()
        # read stdout in the background; reading one pipe while the other fills up would deadlock
        $outTask = $p.StandardOutput.ReadToEndAsync()
        $errTask = $p.StandardError.ReadToEndAsync()  # read stderr in the background as well
        if ($TimeoutSeconds -gt 0) {  # a limit was requested
            # WaitForExit takes milliseconds, so seconds are multiplied by 1000
            if (-not $p.WaitForExit($TimeoutSeconds * 1000)) {
                # report the command line that hung; the finally block kills it
                throw "'$FilePath $(ConvertTo-NarrationArgumentString $ArgumentList)' did not finish within $TimeoutSeconds s"
            }
        }
        $p.WaitForExit()  # wait without a limit (returns at once when the timed wait already saw the exit)
        [pscustomobject]@{  # return the exit code and both captured text streams as one object
            ExitCode = $p.ExitCode  # the program's exit status (0 normally means success)
            # .Result waits for the reader to finish; NUL characters are dropped because wsl.exe can emit UTF-16 text,
            # which shows NULs when decoded as UTF-8
            StdOut   = $outTask.Result.Replace([string][char]0, '')
            StdErr   = $errTask.Result.Replace([string][char]0, '')  # the same clean-up for the error stream
        }
    }
    finally {  # runs on success, on timeout and on Ctrl+C
        Stop-NarrationProcess -Process $p  # kill it if it is still running
        $p.Dispose()  # release the process handle
    }
}

# Runs a program attached to this console, so its output, progress bars and prompts (winget agreements, UAC,
# the WSL user name) appear directly and it can read keyboard input. Parameters as for New-NarrationStartInfo.
# Returns the exit code (int). Used by Install-NarrationDependency and Start-NarrationMcpServer.
function Invoke-NarrationNative {
    # Runs a program attached to this console's stdin/stdout/stderr (installers, the MCP server).
    param(
        # required: the installer or server to start, by name or full path
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @(),  # its arguments, passed individually; an empty list by default
        [System.Collections.IDictionary]$Environment = @{}  # environment changes for the child
    )
    # no -Redirect: the child shares this console's stdin, stdout and stderr
    $psi = New-NarrationStartInfo -FilePath $FilePath -ArgumentList $ArgumentList -Environment $Environment
    # start the program sharing this console (throws a clear message if it is missing)
    $p = Start-NarrationProcess -StartInfo $psi
    try {  # clean up even if the user presses Ctrl+C
        $p.WaitForExit()  # block until the program ends
        $p.ExitCode  # the exit code is the function's output
    }
    finally {  # runs whether the program ended normally, failed, or Ctrl+C stopped the script
        Stop-NarrationProcess -Process $p  # kill the child if PowerShell is stopping before it ended
        $p.Dispose()  # free the operating system handle held by the Process object
    }
}

# Tells whether a program can be started: an existing file path, or an executable found on PATH.
# -Name is the program name or path. Returns $true or $false. Used before probing Python, WSL and installers.
function Test-NarrationCommand {
    param([string]$Name)  # program name or path
    if (Test-Path -LiteralPath $Name -PathType Leaf) { return $true }  # a direct path to an existing file counts
    # otherwise look on PATH; -CommandType Application ignores functions and aliases that share the name
    [bool](Get-Command -Name $Name -CommandType Application -ErrorAction SilentlyContinue)
}

# Finds the Python that runs the toolkit natively and returns a [pscustomobject] with Executable (full path),
# Version (for example 3.13.1) and Command (what was tried). -Refresh ignores the cached answer, which
# Install-NarrationDependency uses after installing Python. Starts each candidate once with a tiny probe script and
# throws with the list of everything tried when none works. The answer is cached in $script:Cache.Python.
# Order: an explicit choice (Set-NarrationBackend -Python, else $env:SPEECHKIT_PYTHON) always wins and replaces
# discovery; otherwise py -3.13, py -3, python.exe and the default install folders on Windows, python3 and python
# elsewhere.
function Resolve-NarrationPython {
    [CmdletBinding()]
    param([switch]$Refresh)  # -Refresh forces a new search instead of reusing the cached answer
    # reuse the earlier answer: probing starts a process and can take a second
    if ($script:Cache.Python -and -not $Refresh) { return $script:Cache.Python }
    # ordered candidates; each is an array of program plus leading arguments
    $candidates = New-Object System.Collections.Generic.List[object]
    # a Python chosen with Set-NarrationBackend -Python takes priority over everything else
    $explicit = $script:Config.Python
    if (-not $explicit) { $explicit = $env:SPEECHKIT_PYTHON }  # then the SPEECHKIT_PYTHON environment variable
    if ($explicit) {  # an explicit choice is the only candidate: it is never second-guessed by discovery
        # an existing file path is one token even when it contains spaces
        if (Test-Path -LiteralPath $explicit -PathType Leaf) { $candidates.Add(@($explicit)) }
        # otherwise a command line such as 'py -3.13' is split into tokens
        else { $candidates.Add((Split-NarrationCommandLine $explicit)) }
    }
    # no explicit choice on Windows: try the py launcher, PATH and the default install folders
    elseif ($script:OnWindows) {
        $candidates.Add(@('py', '-3.13'))  # the py launcher asking for 3.13, the version the toolkit targets
        $candidates.Add(@('py', '-3'))  # any Python 3 the launcher knows about
        # python.exe on PATH (may be the Microsoft Store stub, which the probe below rejects)
        $candidates.Add(@('python.exe'))
        # Look in the per-user and all-users install folders too, in case PATH was not refreshed yet. The loop variable
        # must not be called $base or similar: PowerShell variable names ignore case, so a name shared with a parameter
        # would overwrite it.
        foreach ($installRoot in @($env:LOCALAPPDATA, $env:ProgramFiles)) {
            if ($installRoot) {  # skip a variable that is not set
                # the per-user installer's default under %LOCALAPPDATA%
                $candidates.Add(@((Join-Path $installRoot 'Programs\Python\Python313\python.exe')))
                # the all-users installer's default under %ProgramFiles%
                $candidates.Add(@((Join-Path $installRoot 'Python313\python.exe')))
            }
        }
    }
    else {  # Linux and macOS discovery
        $candidates.Add(@('python3'))  # the usual name for Python 3
        $candidates.Add(@('python'))  # fallback name on systems where python is Python 3
    }
    # one-line script printing the real interpreter path and the version as major.minor.micro
    $probe = 'import sys; print(sys.executable); print("%d.%d.%d" % sys.version_info[:3])'
    $tried = @()  # everything tried, for the error message
    foreach ($c in $candidates) {  # try candidates in order and stop at the first that works
        $exe = $c[0]  # the first token is the program to start, e.g. py or a full python.exe path
        $rest = @($c | Select-Object -Skip 1)  # its leading arguments, such as -3.13
        $tried += ($c -join ' ')  # remember the command line for the error message
        if (-not (Test-NarrationCommand $exe)) { continue }  # skip programs that do not exist at all
        try {  # a broken interpreter must not stop the search
            # run the probe; 60 s allows for a slow first start under antivirus scanning
            $r = Invoke-NarrationProcess -FilePath $exe -ArgumentList ($rest + @('-c', $probe)) -TimeoutSeconds 60
        }
        catch { continue }  # could not start or timed out: try the next one
        if ($r.ExitCode -ne 0) { continue }  # a non-zero exit means this Python is unusable
        # non-blank output lines, accepting both CRLF and LF endings
        $lines = @($r.StdOut -split "`r?`n" | Where-Object { $_.Trim() })
        if ($lines.Count -lt 2) { continue }  # both the path and the version line are needed
        $resolved = $lines[0].Trim()  # sys.executable, the real python.exe even when the py launcher was used
        # fall back to the command name if Python reported an odd or empty path
        if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) { $resolved = $exe }
        $found = [pscustomobject]@{  # the answer for this search
            Executable = $resolved  # full path used to launch the toolkit from now on
            Version    = $lines[1].Trim()  # version string, shown by Get-NarrationBackend and the doctor rows
            Command    = ($c -join ' ')  # the candidate that worked, for messages
        }
        $script:Cache.Python = $found  # kept for the session
        # visible with -Verbose: which Python was chosen and why
        Write-Verbose "speechkit-narrate: Python $($found.Version) at $resolved (from '$($found.Command)')"
        return $found  # stop at the first working candidate
    }
    # nothing worked: list what was tried and how to fix it (the message continues on the next line)
    throw ("No working Python found (tried: $($tried -join ', ')). Install Python 3.13 with " +
           "Install-NarrationDependency, or point the module at one: Set-NarrationBackend -Python 'C:\Python313\python.exe'")
}
