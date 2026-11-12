# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
# WSL: the command prefix, path translation with wslpath, the Python inside WSL, and mapping results back.

# where Install-NarrationDependency -Wsl creates the Python venv with the voice packages inside Linux
$script:WslVenvPython = '/opt/speechkit-narrate/venv/bin/python'

# Describes how to start a Linux program in WSL: returns a [pscustomobject] with FilePath (the launcher), Arguments
# (the words that come before the Linux command) and Override (true when $env:SPEECHKIT_WSL supplied it).
# Takes no parameters and starts nothing. Used by every WSL call, by Get-NarrationBackend and to build
# SPEECHKIT_DELEGATE for Auto mode.
function Get-NarrationWslPrefix {
    # How to start a Linux program in WSL. $env:SPEECHKIT_WSL replaces the whole prefix (for example a
    # different launcher, or the test stand-in "python3 tests/fixtures/fake_wsl.py"); otherwise
    # wsl.exe [-d <distro>] --exec. --exec runs the program directly instead of through the Linux shell,
    # so JSON, $, quotes, parentheses and spaces in arguments arrive unchanged.
    if ($env:SPEECHKIT_WSL) {
        # split the override into program and arguments, honouring quotes
        $tokens = Split-NarrationCommandLine $env:SPEECHKIT_WSL
        return [pscustomobject]@{  # the override's launch description
            FilePath  = $tokens[0]  # first token is the program to start
            # the rest come before the Linux command; @() keeps a single word as an array
            Arguments = @($tokens | Select-Object -Skip 1)
            Override  = $true  # flags that a custom launcher is in use
        }
    }
    $wslArgs = @()  # arguments for the real wsl.exe
    # -d picks a distribution; without it WSL uses its default one
    if ($script:Config.Distribution) { $wslArgs += @('-d', $script:Config.Distribution) }
    # --exec runs the program directly, so the Linux shell never re-parses JSON, $ signs, quotes or spaces
    $wslArgs += '--exec'
    $exe = 'wsl.exe'  # bare name, found through PATH when the full path below is not available
    if ($script:OnWindows -and $env:SystemRoot) {  # on Windows prefer the copy in System32
        $full = Join-Path $env:SystemRoot 'System32\wsl.exe'  # %SystemRoot%\System32\wsl.exe, the one Windows installs
        # the full path avoids a different wsl.exe earlier on PATH and works even with a trimmed PATH
        if (Test-Path -LiteralPath $full) { $exe = $full }
    }
    [pscustomobject]@{ FilePath = $exe; Arguments = $wslArgs; Override = $false }  # the default launch description
}

# Tells whether WSL can be used at all. Returns $true when $env:SPEECHKIT_WSL names a stand-in, $false on Linux and
# macOS, and otherwise whether wsl.exe exists. No parameters, no side effects (it does not start WSL).
function Test-NarrationWslAvailable {
    if ($env:SPEECHKIT_WSL) { return $true }  # a custom launcher is trusted to work (the tests rely on this on Linux)
    if (-not $script:OnWindows) { return $false }  # WSL only exists on Windows
    Test-NarrationCommand (Get-NarrationWslPrefix).FilePath  # is the launcher an existing file or on PATH
}

# Returns the SPEECHKIT_DELEGATE value for the Python side: the WSL launcher as one quoted command line, such as
# "C:\Windows\System32\wsl.exe -d Ubuntu-24.04 --exec", or $null when nothing should be delegated.
# -Backend (optional) overrides the session backend. Only Auto mode on Windows delegates. The toolkit's registry.py
# splits the string back into words and appends env, PYTHONPATH and the python command when it runs a Linux-only stage.
function Get-NarrationDelegateCommand {
    # SPEECHKIT_DELEGATE for the Python side in Auto mode on Windows: the WSL prefix as one command line.
    param([string]$Backend)
    if (-not $Backend) { $Backend = $script:Config.Backend }  # default to the session backend
    # Native and Wsl never delegate, and only Windows lacks the Linux-only voice runtime
    if ($Backend -ne 'Auto' -or -not $script:OnWindows) { return $null }
    if ($env:SPEECHKIT_DELEGATE) { return $env:SPEECHKIT_DELEGATE }  # a value the user set by hand wins
    if (-not (Test-NarrationWslAvailable)) { return $null }  # no WSL, nothing to delegate to
    $p = Get-NarrationWslPrefix  # the launcher in use
    # one string, quoted so that paths with spaces survive the round trip
    ConvertTo-NarrationArgumentString -ArgumentList (@($p.FilePath) + @($p.Arguments))
}

# Runs one Linux command inside WSL and waits for it, capturing the output. -ArgumentList is the Linux command and
# its arguments, -TimeoutSeconds the limit in seconds (120 by default, because the first call may boot the WSL VM).
# Returns Invoke-NarrationProcess's object (ExitCode, StdOut, StdErr). Used for wslpath and the venv probe.
function Invoke-NarrationWsl {
    # Runs one Linux command in WSL and captures it.
    param([string[]]$ArgumentList, [int]$TimeoutSeconds = 120)
    $p = Get-NarrationWslPrefix  # the launcher and its leading words
    # Launcher words first, then the Linux command, run captured; the backtick continues the call on the next line.
    Invoke-NarrationProcess -FilePath $p.FilePath -ArgumentList (@($p.Arguments) + $ArgumentList) `
        -Environment @{ WSL_UTF8 = '1' } -TimeoutSeconds $TimeoutSeconds  # WSL_UTF8 makes wsl.exe's own messages UTF-8; the timeout is passed through
}

# Translates a Windows path into the path WSL sees (C:\Books\x.pdf becomes /mnt/c/Books/x.pdf) using `wslpath -a`.
# -Path is an absolute Windows path. Returns the Linux path string. Answers are cached, and each translation also
# teaches the reverse map used to turn result paths back into Windows paths. Throws when wslpath fails.
function ConvertTo-NarrationWslPath {
    # Windows path -> WSL path with `wslpath -a` (cached per WSL prefix).
    param([Parameter(Mandatory = $true)][string]$Path)
    # the same Windows path may map differently with another launcher or distribution, so both are in the key
    $key = "$($env:SPEECHKIT_WSL)|$($script:Config.Distribution)|$Path"
    # already translated in this session
    if ($script:Cache.WslPath.ContainsKey($key)) { return $script:Cache.WslPath[$key] }
    # -a returns an absolute path; 120 s covers a cold WSL start
    $r = Invoke-NarrationWsl -ArgumentList @('wslpath', '-a', $Path) -TimeoutSeconds 120
    $out = @($r.StdOut -split "`r?`n" | Where-Object { $_.Trim() })  # non-blank lines of output
    # a failed command or no output means the path could not be translated
    if ($r.ExitCode -ne 0 -or $out.Count -eq 0) {
        # report the path, the exit code and WSL's own message
        throw "wslpath could not translate '$Path' (exit $($r.ExitCode)): $($r.StdErr.Trim())"
    }
    $wsl = $out[0].Trim()  # the translated path is the first line
    $script:Cache.WslPath[$key] = $wsl  # remember it for the next call
    Add-NarrationWslReverse -WslPath $wsl -Path $Path  # learn the way back as well
    $wsl  # return the Linux path
}

# Records Linux-to-Windows mappings for a translated path and for each parent folder whose name is the same on both
# sides. -WslPath is the Linux form, -Path the Windows form. Returns nothing; fills $script:Cache.WslReverse. This is
# how output files that the toolkit writes next to an input (same folder, new name) are mapped back to Windows.
function Add-NarrationWslReverse {
    # Remembers wsl -> local for the path and for each parent folder whose name agrees on both sides, so files
    # the toolkit writes next to a translated path map back too (C:\Books\x.pdf -> /mnt/c/Books/x.pdf teaches
    # /mnt/c/Books -> C:\Books, /mnt/c -> C:\ stops where the names differ).
    param([string]$WslPath, [string]$Path)
    $w = $WslPath.TrimEnd('/')  # work without a trailing slash
    $p = $Path.TrimEnd([char[]]'\/')  # and without a trailing backslash or slash on the Windows side
    while ($w -and $p) {  # climb both paths together while each still has text
        # keep the first mapping learned for a Linux path; later ones never overwrite it
        if (-not $script:Cache.WslReverse.ContainsKey($w)) { $script:Cache.WslReverse[$w] = $p }
        $cut = $w.LastIndexOf('/')  # position of the last folder separator in the Linux path
        if ($cut -le 0) { break }  # stop before reaching the Linux root (/mnt itself is never mapped)
        $pParent = [System.IO.Path]::GetDirectoryName($p)  # the Windows parent folder; empty or $null at a drive root
        if (-not $pParent) { break }  # nothing left to climb on the Windows side
        # the two paths only correspond while their last names agree; a difference means the trees diverge here
        if ($w.Substring($cut + 1) -ne [System.IO.Path]::GetFileName($p)) { break }
        $w = $w.Substring(0, $cut)  # drop the last Linux path segment
        $p = $pParent  # and the matching Windows segment
    }
}

# Puts an absolute local path into the form the backend's Python needs: unchanged for Native and Auto, translated
# with wslpath for Wsl. -Path is the absolute path, -Backend an optional override. Returns a string.
function ConvertTo-NarrationBackendPath {
    # An absolute local path in the form the chosen backend's Python needs.
    param([Parameter(Mandatory = $true)][string]$Path, [string]$Backend)
    # only a Python running inside WSL needs /mnt/... paths
    if ((Get-NarrationEffectiveBackend $Backend) -eq 'Wsl') { ConvertTo-NarrationWslPath -Path $Path } else { $Path }
}

# Returns the python command to use inside WSL: Set-NarrationBackend -WslPython, then $env:SPEECHKIT_WSL_PYTHON, then
# the venv made by Install-NarrationDependency -Wsl if it exists, else python3. Takes no parameters. The probe starts
# WSL once per session; its answer is cached in $script:Cache.WslPython.
function Resolve-NarrationWslPython {
    # The python inside WSL: Set-NarrationBackend -WslPython, $env:SPEECHKIT_WSL_PYTHON, the venv made by
    # Install-NarrationDependency -Wsl (/opt/speechkit-narrate/venv) when it exists, else python3.
    if ($script:Config.WslPython) { return $script:Config.WslPython }
    if ($env:SPEECHKIT_WSL_PYTHON) { return $env:SPEECHKIT_WSL_PYTHON }  # the environment variable comes next
    if ($script:Cache.WslPython) { return $script:Cache.WslPython }  # already probed in this session
    $py = 'python3'  # the fallback: the distribution's own Python 3
    try {  # probing must never stop the caller
        # `test -x` succeeds when the venv's python exists and is executable; 60 s allows for WSL starting up
        $r = Invoke-NarrationWsl -ArgumentList @('test', '-x', $script:WslVenvPython) -TimeoutSeconds 60
        if ($r.ExitCode -eq 0) { $py = $script:WslVenvPython }  # exit code 0 means the venv is there, so use it
    }
    # WSL missing or not answering: keep python3 and say why under -Verbose
    catch { Write-Verbose "speechkit-voice-tts-offline-pipeline: could not probe WSL for $($script:WslVenvPython): $_" }
    $script:Cache.WslPython = $py  # remember the decision for the session
    $py  # output the chosen python command, which is the function's return value
}

# Rewrites $env:SPEECHKIT_PLUGINS for a Python running inside WSL: entries are split on this platform's path
# separator, .py files are made absolute and translated with wslpath, and the list is joined with ':' as Linux expects.
# Takes no parameters. Returns the string, or $null when no plug-ins are set.
function Get-NarrationWslPluginValue {
    # $env:SPEECHKIT_PLUGINS for the Linux side: .py files translated, joined with ':'.
    if (-not $env:SPEECHKIT_PLUGINS) { return $null }  # nothing to pass on
    # -split takes a regular expression, so the separator (';' on Windows) is escaped before use
    $items = foreach ($spec in ($env:SPEECHKIT_PLUGINS -split [regex]::Escape([string][System.IO.Path]::PathSeparator))) {
        $s = $spec.Trim()  # trim stray spaces around each entry
        if (-not $s) { continue }  # skip empty entries such as a trailing separator
        # a .py path, or any existing file, must be translated
        if ($s.EndsWith('.py') -or (Test-Path -LiteralPath $s -PathType Leaf)) {
            # absolute first (relative to the current location), then to /mnt/...
            ConvertTo-NarrationWslPath -Path (Resolve-NarrationPath $s)
        }
        else { $s }  # module names need no translation
    }
    (@($items) -join ':')  # Linux's os.pathsep is ':'
}

# Rewrites Linux paths inside a parsed result back into Windows paths, recursing through objects and arrays.
# -Value is any part of the result; -Pairs holds (linux prefix, windows path) pairs sorted longest first so the most
# specific prefix wins. Strings under /mnt/<drive>/ that were never translated are converted directly on Windows.
# Returns the rewritten value; PSCustomObjects are changed in place.
function ConvertFrom-NarrationWslValue {
    # Maps WSL paths in a result back to the Windows paths they came from (and /mnt/<drive>/... on Windows).
    param($Value, [object[]]$Pairs)
    if ($null -eq $Value) { return $null }  # a JSON null holds no path to translate, so it comes back as $null
    if ($Value -is [string]) {  # strings are where paths live
        foreach ($pair in $Pairs) {  # try each known mapping, most specific first
            $w = $pair[0]  # the Linux prefix of this pair
            # an exact match maps to the Windows path itself; -ceq because Linux paths are case-sensitive
            if ($Value -ceq $w) { return $pair[1] }
            # a path inside the mapped folder; the trailing '/' stops /mnt/c/Books matching /mnt/c/Bookshelf
            if ($Value.StartsWith($w + '/', [System.StringComparison]::Ordinal)) {
                # the part below the prefix, with '/' turned into the local separator ('\' on Windows)
                $rest = $Value.Substring($w.Length).Replace('/', [string][System.IO.Path]::DirectorySeparatorChar)
                # joined to the Windows folder without doubling the separator
                return $pair[1].TrimEnd([char[]]'\/') + $rest
            }
        }
        # a /mnt/<drive>/... path the module never translated, such as a folder the toolkit chose itself
        if ($script:OnWindows -and $Value -match '^/mnt/([a-zA-Z])(/.*)?$') {
            $tail = ''  # the part after the drive letter
            if ($Matches[2]) { $tail = $Matches[2].Replace('/', '\') }  # convert its separators when there is one
            # drive letter in upper case plus a colon, then the rest, or a backslash for the bare drive
            return $Matches[1].ToUpperInvariant() + ':' + $(if ($tail) { $tail } else { '\' })
        }
        return $Value  # any other string (text, or a Linux-only path such as /opt/...) is left alone
    }
    if ($Value -is [System.Management.Automation.PSCustomObject]) {  # an object from ConvertFrom-Json
        foreach ($prop in $Value.PSObject.Properties) {  # each property of the object, rewritten in place
            # replace the value with its rewritten form
            $prop.Value = ConvertFrom-NarrationWslValue -Value $prop.Value -Pairs $Pairs
        }
        return $Value  # the same object, now holding Windows paths
    }
    if ($Value -is [System.Array]) {  # a JSON array, whose elements are rewritten one by one
        # the leading comma keeps a nested array as one element instead of merging it into the outer list
        $list = foreach ($item in $Value) { , (ConvertFrom-NarrationWslValue -Value $item -Pairs $Pairs) }
        # return the array as one object; note that a one-element array whose element is itself an array comes back
        # flattened
        return , @($list)
    }
    $Value  # numbers, booleans and other values pass through
}

# Maps every Linux path in a whole result object back to Windows, using everything ConvertTo-NarrationWslPath has
# learned this session. -Object is the parsed JSON result. Returns the rewritten result. Called by
# Invoke-NarrationCli when the Wsl backend ran the command.
function ConvertFrom-NarrationWslOutput {
    param($Object)  # the result from Python
    # the reverse map as pairs, longest Linux prefix first so /mnt/c/Books/x wins over /mnt/c
    $pairs = @($script:Cache.WslReverse.GetEnumerator() | Sort-Object { $_.Key.Length } -Descending |
            ForEach-Object { , @($_.Key, $_.Value) })  # the comma makes each key/value pair a single two-item element
    ConvertFrom-NarrationWslValue -Value $Object -Pairs $pairs  # rewrite the result recursively
}
