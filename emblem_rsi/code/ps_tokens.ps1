# Tokenize PowerShell sources for the code-comment audit (emblem_rsi/code).
# Usage: pwsh -NoProfile -File ps_tokens.ps1 <request.json>   (request: a JSON array of file paths)
# Prints one JSON object: { "<path>": { "errors": [...], "tokens": [[kind, text, sl, sc, el, ec, flags]] } }
# The single argument: the path of a JSON request file listing the scripts to tokenize. It is mandatory, so a call
# without it fails at once instead of prompting.
param([Parameter(Mandatory = $true)][string]$Request)
$ErrorActionPreference = 'Stop'  # turn every error into a terminating one, so failures give a non-zero exit code
# read the request file as one UTF-8 string and decode the array of paths
$paths = Get-Content -LiteralPath $Request -Raw -Encoding UTF8 | ConvertFrom-Json
$result = [ordered]@{}  # ordered dictionary, so the reply lists files in request order
foreach ($p in $paths) {  # tokenize each requested file in turn
    $tokens = $null  # receives the token array from ParseFile through a [ref] argument
    $errors = $null  # receives the array of parse errors the same way
    # Parse the file with PowerShell's own language parser; [void] discards the returned syntax tree, because only the
    # tokens and errors are needed.
    [void][System.Management.Automation.Language.Parser]::ParseFile($p, [ref]$tokens, [ref]$errors)
    $list = New-Object System.Collections.Generic.List[object]  # growable list for the token records of this file
    foreach ($t in $tokens) {  # every token, comments and line breaks included
        $e = $t.Extent  # the token's extent: its exact text and start and end positions
        # kind name, exact text, start line and column, end line
        $list.Add(@([string]$t.Kind, $e.Text, $e.StartLineNumber, $e.StartColumnNumber, $e.EndLineNumber,
                    # end column, then the token flags as text (unused by Python)
                    $e.EndColumnNumber, [string]$t.TokenFlags))
    }
    # This file's entry: each parse error message with its line number, and the token list.
    $result[$p] = [ordered]@{ errors = @($errors | ForEach-Object { $_.Message + ' (line ' + $_.Extent.StartLineNumber + ')' }); tokens = $list }
}
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8  # emit UTF-8 so non-ASCII token text reaches Python intact
$result | ConvertTo-Json -Depth 6 -Compress  # one compact JSON object; depth 6 covers the nested token arrays
