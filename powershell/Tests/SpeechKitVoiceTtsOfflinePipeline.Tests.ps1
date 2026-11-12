# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
#Requires -Version 7.2
# Pester 5+ tests for the SpeechKitVoiceTtsOfflinePipeline module. Run from the checkout:
#   pwsh -NoProfile -Command "Invoke-Pester ./powershell/Tests -Output Detailed"

Describe 'SpeechKitVoiceTtsOfflinePipeline module' {
    # Runs once, before the first Context: finds the module and the checkout, saves and clears the SPEECHKIT_*
    # variables, imports the module (capturing any warning), and builds the shared fixtures: a small text, a plug-in
    # with a stage and a tool, a project path and a deliberately awkward title. Everything is stored in $script:
    # variables so It blocks see it.
    BeforeAll {  # Pester 5: setup code must live in BeforeAll, not loose in the Describe body
        # the module folder next to this Tests folder, as an absolute path
        $script:ModuleDir = (Resolve-Path (Join-Path $PSScriptRoot '../SpeechKitVoiceTtsOfflinePipeline')).Path
        # the manifest is what Import-Module and Test-ModuleManifest read
        $script:Manifest = Join-Path $ModuleDir 'SpeechKitVoiceTtsOfflinePipeline.psd1'
        # checkout root: powershell/Tests/../.. holds the speechkit_narrate package
        $script:Root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
        # stand-in for wsl.exe used by the WSL and Auto contexts (shared with the Python tests)
        $script:FakeWsl = Join-Path $Root 'tests/narration/fixtures/fake_wsl.py'
        # Every environment variable that changes how the module or the toolkit behaves. A developer's own settings
        # (a SPEECHKIT_BACKEND=Wsl, a plug-in list, a forced platform, a PYTHONPATH) would otherwise change the results.
        $script:EnvNames = @('SPEECHKIT_PLUGINS', 'SPEECHKIT_WSL', 'FAKE_WSL_PREFIX', 'FAKE_WSL_LOG', 'SPEECHKIT_PYTHON',
            'SPEECHKIT_PLATFORM', 'SPEECHKIT_DELEGATE', 'SPEECHKIT_DELEGATE_PYTHON', 'SPEECHKIT_DELEGATE_ROOT',
            'SPEECHKIT_BACKEND', 'SPEECHKIT_WSL_DISTRO', 'SPEECHKIT_WSL_PYTHON', 'SPEECHKIT_ROOT', 'PYTHONPATH')
        $script:SavedEnv = @{}  # name -> original value, restored in AfterAll
        foreach ($k in $EnvNames) {  # for each variable that steers the module or the toolkit
            $SavedEnv[$k] = [Environment]::GetEnvironmentVariable($k)  # remember the current value (null when unset)
            Remove-Item -LiteralPath "Env:$k" -ErrorAction SilentlyContinue  # remove it for the duration of the tests
        }
        # Import the module freshly (-Force) and keep only warnings and errors: 3>&1 and 2>&1 merge those streams into
        # the pipeline, and the filter drops the PSModuleInfo that -PassThru emits. The module warns when it cannot
        # generate its per-tool commands, so an empty result means Python and the registry worked at import.
        $script:ImportOutput = @(Import-Module $Manifest -Force -PassThru 3>&1 2>&1 | Where-Object {
                $_ -is [System.Management.Automation.WarningRecord] -or $_ -is [System.Management.Automation.ErrorRecord] })
        # the Python the module found (its sys.executable), reused to make fixtures
        $script:Py = (Get-NarrationBackend).Python

        # Shared across Contexts, so not in TestDrive (Pester removes what a Context adds there when it ends).
        # A random 8-hex-digit suffix keeps parallel or leftover runs apart.
        $script:WorkBase = Join-Path ([System.IO.Path]::GetTempPath()) "skn-ps-tests-$([guid]::NewGuid().ToString('n').Substring(0, 8))"
        $script:Work = Join-Path $WorkBase 'work dir'  # a folder name with a space, to exercise quoting everywhere
        New-Item -ItemType Directory -Path $Work -Force | Out-Null  # create it (and WorkBase)
        $script:BookTxt = Join-Path $Work 'small book.txt'  # the input file name also contains a space
        # The test text in the toolkit's markup: a title line, two "# n. ..." chapter headings and four paragraphs
        # (about 70 words), enough for every stage the tests run.
        $book = @(
            'The Small Test Book'
            ''
            '# 1. The Beginning'
            ''
            'It was a quiet morning in the garden. The birds sang softly and the wind moved through the tall grass.'
            ''
            'She walked to the gate and looked down the long road toward the village.'
            ''
            '# 2. The Middle'
            ''
            'The afternoon brought rain, steady and warm, and the children ran inside laughing.'
            ''
            'By evening the clouds had cleared and the first stars appeared above the hills.'
        ) -join "`n"  # LF line endings, as on Linux
        # UTF-8 without a byte-order mark; a BOM would end up in the first line Python reads
        [System.IO.File]::WriteAllText($BookTxt, $book + "`n", (New-Object System.Text.UTF8Encoding($false)))

        # A plug-in with a stage "stats" (needs script, option wpm) and a tool "wordcount" (one required path).
        $script:StatsPlugin = Join-Path $Work 'reading_stats.py'
        $plugin = @(
            'import json'
            'from speechkit_narrate.registry import stage, tool'
            ''
            ''
            '@stage("stats", needs=["script"], outputs=["out/stats.json"], description="reading statistics",'
            '       options={"wpm": {"type": "integer", "minimum": 1, "default": 150, "description": "words per minute"}})'
            'def stats(project, ctx):'
            '    words = len((project.root / "work" / "book.md").read_text(encoding="utf-8").split())'
            '    wpm = ctx.opt("wpm", 150)'
            '    (project.root / "out").mkdir(exist_ok=True)'
            '    data = {"words": words, "wpm": wpm, "reading_minutes": round(words / wpm, 2)}'
            '    (project.root / "out" / "stats.json").write_text(json.dumps(data), encoding="utf-8")'
            '    return data'
            ''
            ''
            '@tool("wordcount", params={"file": {"type": "string", "format": "path", "description": "a text file"}},'
            '      required=["file"], description="count the words of a text file")'
            'def wordcount(file, log=print):'
            '    with open(file, encoding="utf-8") as f:'
            '        return {"files": [], "words": len(f.read().split())}'
        ) -join "`n"
        [System.IO.File]::WriteAllText($StatsPlugin, $plugin + "`n")

        # the main project folder (created by the first Project workflow test)
        $script:Project = Join-Path $Work 'my book'
        # A title with an apostrophe (doubled inside single quotes), double quotes, parentheses, a dollar sign and an
        # ampersand: each one breaks naive quoting in cmd.exe, PowerShell or a Linux shell.
        $script:Title = 'It''s a "Test" (1) $5 & co'  # the value is: It's a "Test" (1) $5 & co
        # where the planner records each stage run
        $script:StateFile = 'work/.speechkit_narrate/state.json'

        # Starts a program as an MCP stdio server, sends one JSON-RPC initialize request and returns the parsed first
        # reply. Throws when no reply comes within 90 s. Starts a process and stops it again.
        function script:Invoke-McpInitialize([string]$FilePath, [string[]]$Arguments, [hashtable]$Environment = @{}) {
            $psi = New-Object System.Diagnostics.ProcessStartInfo $FilePath
            foreach ($a in $Arguments) { $psi.ArgumentList.Add($a) }  # no quoting needed with ArgumentList
            foreach ($k in $Environment.Keys) { $psi.Environment[$k] = [string]$Environment[$k] }  # e.g. a registered env block
            $psi.UseShellExecute = $false  # start the program directly, not through the shell
            $psi.RedirectStandardInput = $true  # the request is written to its stdin
            $psi.RedirectStandardOutput = $true  # the reply is read from its stdout
            $psi.RedirectStandardError = $true  # stderr is captured too (the server logs there)
            $p = [System.Diagnostics.Process]::Start($psi)
            try {  # the process is always stopped in finally
                $null = $p.StandardError.ReadToEndAsync()  # drain stderr so a chatty server cannot block on a full pipe
                # MCP's stdio transport is newline-delimited JSON; id 1 lets the test match the reply
                $p.StandardInput.WriteLine('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"pester","version":"1"}}}')
                $p.StandardInput.Flush()  # push the line to the server now
                $read = $p.StandardOutput.ReadLineAsync()  # read the first stdout line without blocking forever
                if (-not $read.Wait(90000)) { throw 'no reply from the MCP server within 90 s' }
                $read.Result | ConvertFrom-Json  # the reply as an object (jsonrpc, id, result)
            }
            finally {
                try { $p.StandardInput.Close() } catch { Write-Verbose "already closed: $($_.Exception.Message)" }
                if (-not $p.WaitForExit(20000)) { $p.Kill($true) }  # wait up to 20 s, then kill the whole tree
            }
        }

        # Returns $true when the given parameter of a command is mandatory in any parameter set.
        function script:Get-Mandatory($Command, $Parameter) {
            @((Get-Command $Command).Parameters[$Parameter].Attributes |
                    Where-Object { $_ -is [System.Management.Automation.ParameterAttribute] } |
                    ForEach-Object { $_.Mandatory }) -contains $true
        }

        # Writes a stand-in python: a shell script that answers the module's probe (`-c ...`) with its own path and
        # version 3.13.0 and exits 0; any other call prints a usage line to stderr and exits 2. Returns its path.
        function script:New-FakePython([string]$Path) {
            New-Item -ItemType Directory -Path (Split-Path -Parent $Path) -Force | Out-Null
            # Set-Content in PowerShell 7 writes no BOM, so the #! line works
            Set-Content -LiteralPath $Path -NoNewline -Value ("#!/bin/sh`nif [ `"`$1`" = '-c' ]; then echo `"$Path`"; echo 3.13.0; exit 0; fi`n" +
                                                             "echo 'usage: fake python broken here' >&2`nexit 2")
            & chmod +x $Path  # make it executable (these tests need Linux or macOS)
            $Path
        }

        # Runs a PowerShell script in a child pwsh that has the module imported, and returns what the script wrote to
        # stdout. -Environment adds variables for the child only. A non-zero exit throws with the child's stderr.
        function script:Invoke-ChildPwsh([string]$Script, [hashtable]$Environment = @{}) {
            $pwsh = (Get-Process -Id $PID).Path  # the pwsh running these tests
            $prelude = "`$ErrorActionPreference = 'Stop'`nImport-Module '$Manifest' -Force 3>`$null`n"
            $psi = New-Object System.Diagnostics.ProcessStartInfo $pwsh
            foreach ($a in @('-NoProfile', '-NonInteractive', '-Command', ($prelude + $Script))) { $psi.ArgumentList.Add($a) }
            foreach ($k in $Environment.Keys) { $psi.Environment[$k] = [string]$Environment[$k] }
            $psi.UseShellExecute = $false
            $psi.WorkingDirectory = $WorkBase
            $psi.RedirectStandardOutput = $true
            $psi.RedirectStandardError = $true
            $p = [System.Diagnostics.Process]::Start($psi)
            $errTask = $p.StandardError.ReadToEndAsync()  # read both pipes in the background so neither can fill up
            $outTask = $p.StandardOutput.ReadToEndAsync()
            if (-not $p.WaitForExit(300000)) { $p.Kill($true); throw 'the child pwsh did not finish within 5 minutes' }
            $p.WaitForExit()  # let the asynchronous readers finish
            if ($p.ExitCode -ne 0) { throw "child pwsh exited with $($p.ExitCode): $($errTask.Result) $($outTask.Result)" }
            $outTask.Result
        }
    }

    # Runs once, after the last Context: restores the saved variables, unloads the module (its OnRemove handler
    # also removes the generated Invoke-Narration* tool commands) and deletes the shared work folder.
    AfterAll {
        foreach ($k in $EnvNames) {
            if ($null -eq $SavedEnv[$k]) { Remove-Item -LiteralPath "Env:$k" -ErrorAction SilentlyContinue }
            else { Set-Item -LiteralPath "Env:$k" -Value $SavedEnv[$k] }
        }
        Remove-Module SpeechKitVoiceTtsOfflinePipeline -Force -ErrorAction SilentlyContinue
        if ($WorkBase -and (Test-Path -LiteralPath $WorkBase)) { Remove-Item -LiteralPath $WorkBase -Recurse -Force }
    }

    # Module: packaging and code-quality checks that need no Python work beyond the import.
    Context 'Module' {
        It 'has a valid manifest with the new identity' {
            $m = Test-ModuleManifest -Path $Manifest -ErrorAction Stop
            $m.Name | Should -Be 'SpeechKitVoiceTtsOfflinePipeline'
            $m.Version | Should -Be ([version]'1.1.0')
            $m.Guid | Should -Not -Be ([guid]'da5586db-d9b2-4d16-8279-92bc643dcc93')  # not the module it came from
            $m.Author | Should -Be 'EMBLEM-NLP'
            $m.CompanyName | Should -Be 'EMBLEM-NLP'
            [string]$m.ProjectUri | Should -Be 'https://github.com/EMBLEM-NLP/speechkit-voice-tts-offline-pipeline'
            $m.ExportedFunctions.Keys | Should -Contain 'Invoke-NarrationPipeline'
            $m.ExportedAliases.Keys | Should -Contain 'Invoke-Narration'
        }

        It 'imports without warnings or errors' {
            $ImportOutput | Should -BeNullOrEmpty  # the records captured in BeforeAll
        }

        It 'exports the public commands and the Invoke-Narration alias, and nothing private' {
            $expected = @('Get-NarrationBackend', 'Set-NarrationBackend', 'Get-NarrationStage', 'Invoke-NarrationStage',
                'Get-NarrationTool', 'Invoke-NarrationTool', 'Update-NarrationCommand', 'New-NarrationProject',
                'Get-NarrationProject', 'Get-NarrationPlan', 'Invoke-NarrationPipeline', 'Get-NarrationSetting',
                'Set-NarrationSetting', 'Test-NarrationEnvironment', 'Install-NarrationDependency',
                'Start-NarrationMcpServer', 'Register-NarrationMcpServer')
            $names = @(Get-Command -Module SpeechKitVoiceTtsOfflinePipeline -CommandType Function | ForEach-Object Name)
            $names | Sort-Object | Should -Be ($expected | Sort-Object)
            (Get-Alias Invoke-Narration).Definition | Should -Be 'Invoke-NarrationPipeline'
            Get-Command Invoke-NarrationCli -ErrorAction SilentlyContinue | Should -BeNullOrEmpty  # private stays private
            # the two names the Python side prints in its advice must exist exactly
            Get-Command Set-NarrationBackend -ErrorAction Stop | Should -Not -BeNullOrEmpty
            Get-Command Install-NarrationDependency -ErrorAction Stop | Should -Not -BeNullOrEmpty
            # the names the Python side prints really are this module's commands
            $env = Get-Content -Raw (Join-Path $Root 'speechkit_narrate/env.py')
            $env | Should -Match 'Set-NarrationBackend Auto'
            $env | Should -Match 'Install-NarrationDependency -Wsl'
        }

        It 'uses approved verbs and has help with PowerShell examples on every public function' {
            $verbs = @(Get-Verb | ForEach-Object Verb)
            foreach ($c in @(Get-Command -Module SpeechKitVoiceTtsOfflinePipeline -CommandType Function)) {
                $verbs | Should -Contain $c.Verb -Because $c.Name
                $help = Get-Help $c.Name -Full
                $help.Synopsis | Should -Not -BeNullOrEmpty -Because $c.Name
                @($help.Examples.Example).Count | Should -BeGreaterThan 0 -Because $c.Name
                ($help.Examples.Example[0].Code + ($help.Examples.Example[0].Remarks | Out-String)) | Should -Match 'Narration' -Because $c.Name
            }
        }

        It 'carries the SPDX license and copyright header in every PowerShell file' {
            $files = @(Get-ChildItem -Path $ModuleDir -Recurse -Include '*.ps1', '*.psm1', '*.psd1') + @(Get-Item -LiteralPath $PSCommandPath)
            foreach ($f in $files) {
                $head = @(Get-Content -LiteralPath $f.FullName -TotalCount 2)
                $head[0] | Should -BeExactly '# SPDX-License-Identifier: GPL-3.0-or-later' -Because $f.Name
                $head[1] | Should -BeExactly '# Copyright (C) 2026 EMBLEM-NLP' -Because $f.Name
            }
        }

        It 'keeps no trace of the removed features or the old module name' {
            # Private/Package.ps1 (bundled-wheel packaging) is gone; no old file names remain
            Join-Path $ModuleDir 'Private/Package.ps1' | Should -Not -Exist
            @(Get-ChildItem -Path $ModuleDir -Recurse -Filter 'Bookcast*').Count | Should -Be 0
            $bad = @()
            foreach ($f in @(Get-ChildItem -Path $ModuleDir -Recurse -File)) {
                $text = [System.IO.File]::ReadAllText($f.FullName)
                # the old name, and words only the removed features used
                foreach ($word in @('bookcast', 'typeset', 'impose', 'booklet', 'weasyprint', 'playwright', 'chromium', 'tesseract', 'poppler', 'wheels', 'worker queue')) {
                    if ($text -match [regex]::Escape($word)) { $bad += "$($f.Name): $word" }
                }
            }
            $bad | Should -BeNullOrEmpty
        }

        It 'mentions the old module only once in the README and shows no bash' {
            $readme = Get-Content -Raw (Join-Path $ModuleDir '../README.md')
            ([regex]::Matches($readme, 'bookcast', 'IgnoreCase')).Count | Should -Be 1
            $readme | Should -Not -Match '```(bash|sh|shell)\b'
            $readme | Should -Match '```powershell'
        }

        It 'uses no syntax that Windows PowerShell 5.1 lacks (??, ?., ternary, && and ||, Join-Path with three parts)' {
            $bad = @()
            foreach ($f in @(Get-ChildItem -Path $ModuleDir -Recurse -Include '*.ps1', '*.psm1')) {
                $tokens = $null; $errors = $null
                $ast = [System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$tokens, [ref]$errors)
                $errors | Should -BeNullOrEmpty -Because $f.Name
                $paramAst = [System.Management.Automation.Language.CommandParameterAst]
                $found = $ast.FindAll({
                        param($n)
                        $n -is [System.Management.Automation.Language.TernaryExpressionAst] -or
                        $n -is [System.Management.Automation.Language.PipelineChainAst] -or
                        ($n -is [System.Management.Automation.Language.BinaryExpressionAst] -and $n.Operator -eq 'QuestionQuestion') -or
                        ($n -is [System.Management.Automation.Language.AssignmentStatementAst] -and $n.Operator -eq 'QuestionQuestionEquals') -or
                        ($n -is [System.Management.Automation.Language.MemberExpressionAst] -and $n.NullConditional) -or
                        ($n -is [System.Management.Automation.Language.CommandAst] -and
                        $n.GetCommandName() -eq 'Join-Path' -and
                        @($n.CommandElements | Select-Object -Skip 1 | Where-Object { $_ -isnot $paramAst }).Count -gt 2)
                    }, $true)
                foreach ($x in $found) { $bad += "$($f.Name):$($x.Extent.StartLineNumber) $($x.Extent.Text)" }
            }
            $bad | Should -BeNullOrEmpty
        }

        It 'contains only ASCII characters in every module file and in these tests' {
            $bad = @()
            foreach ($f in @(Get-ChildItem -Path $ModuleDir -Recurse -File) + @(Get-Item -LiteralPath $PSCommandPath)) {
                $m = [regex]::Match([System.IO.File]::ReadAllText($f.FullName, [System.Text.Encoding]::UTF8), '[^\x00-\x7F]')
                if ($m.Success) { $bad += "$($f.Name): U+$(([int][char]$m.Value[0]).ToString('X4'))" }
            }
            $bad | Should -BeNullOrEmpty
        }

        It 'quotes arguments for Windows PowerShell 5.1 so they parse back unchanged' {
            $cases = @('plain', 'with space', 'quote"inside', 'trail\', 'C:\Program Files\x\', '{"a": "b c", "d": [1, 2]}', '', 'a\\"b', "it's (1) `$5 & co")
            $line = InModuleScope SpeechKitVoiceTtsOfflinePipeline -Parameters @{ Cases = $cases } { param($Cases) ConvertTo-NarrationArgumentString -ArgumentList $Cases }
            $psi = New-Object System.Diagnostics.ProcessStartInfo
            $psi.FileName = $Py
            $psi.Arguments = '-c "import json,sys; print(json.dumps(sys.argv[1:]))" ' + $line
            $psi.UseShellExecute = $false
            $psi.RedirectStandardOutput = $true
            $p = [System.Diagnostics.Process]::Start($psi)
            $out = $p.StandardOutput.ReadToEnd()
            $p.WaitForExit()
            $parsed = @($out | ConvertFrom-Json)
            $parsed.Count | Should -Be $cases.Count
            for ($i = 0; $i -lt $cases.Count; $i++) { $parsed[$i] | Should -BeExactly $cases[$i] }
        }

        It 'parses JSON with the -Depth the engine supports (6.2 and later)' {
            # 60 levels of nesting: more than the old default of ConvertFrom-Json in some engines, less than 100
            $deep = ('{"a":' * 60) + '1' + ('}' * 60)
            $o = InModuleScope SpeechKitVoiceTtsOfflinePipeline -Parameters @{ J = $deep } { param($J) ConvertFrom-NarrationJson -Json $J }
            $o | Should -Not -BeNullOrEmpty
            # the check compares the full version, so 6.0 and 6.1 (no -Depth) take the other branch
            $src = Get-Content -Raw (Join-Path $ModuleDir 'Private/Invoke.ps1')
            $src | Should -Match ([regex]::Escape("`$PSVersionTable.PSVersion -ge [version]'6.2'"))
        }
    }

    # Backend: what Get-NarrationBackend reports by default and how Set-NarrationBackend validates its input.
    Context 'Legacy module compatibility' {
        It 'imports the legacy module in a fresh shell with canonical and legacy type names' {
            $legacy = Join-Path $Root 'powershell/SpeechKitNarrate/SpeechKitNarrate.psd1'
            $pwsh = (Get-Process -Id $PID).Path
            $code = "Import-Module '$legacy' -ErrorAction Stop; `$b = Get-NarrationBackend -NoProbe; [pscustomobject]@{ Types = @(`$b.PSObject.TypeNames); Mode = `$b.Mode; Command = (Get-Command Invoke-NarrationPipeline).Name } | ConvertTo-Json -Compress"
            $reply = ((& $pwsh -NoProfile -NonInteractive -Command $code) | Out-String) | ConvertFrom-Json
            $reply.Types | Should -Contain 'SpeechKitVoiceTtsOfflinePipeline.Backend'
            $reply.Types | Should -Contain 'SpeechKitNarrate.Backend'
            $reply.Mode | Should -Be 'Source'
            $reply.Command | Should -Be 'Invoke-NarrationPipeline'
        }

        It 'shares backend state when both module names are imported' {
            $legacy = Join-Path $Root 'powershell/SpeechKitNarrate/SpeechKitNarrate.psd1'
            $code = "Import-Module '$legacy' -ErrorAction Stop; Set-NarrationBackend -Distribution 'Compat-Shared'; (SpeechKitVoiceTtsOfflinePipeline\Get-NarrationBackend -NoProbe).Distribution"
            (Invoke-ChildPwsh $code | Out-String).Trim() | Should -Be 'Compat-Shared'
        }
    }

    Context 'Backend' {
        It 'defaults to Auto in Source mode with a working native Python' {
            $b = Get-NarrationBackend
            $b.PSObject.TypeNames[0] | Should -Be 'SpeechKitVoiceTtsOfflinePipeline.Backend'
            $b.Backend | Should -Be 'Auto'
            $b.Effective | Should -Be 'Native'
            $b.Mode | Should -Be 'Source'
            $b.Python | Should -Exist
            $b.ToolkitRoot | Should -Be $Root  # two folders above the module, since SPEECHKIT_ROOT is cleared
        }

        # Bug fix: -ToolkitRoot used to be checked after the other settings were stored, so a bad folder left those
        # changes applied with stale caches. Now everything is validated first and nothing changes on a refusal.
        It 'refuses a bad toolkit root without changing any other setting or the caches' {
            $before = InModuleScope SpeechKitVoiceTtsOfflinePipeline { Resolve-NarrationPython | Out-Null; $script:Cache.Python }  # fill the cache
            { Set-NarrationBackend -Backend Native -Python '/no/such/python' -WslPython 'nope' -Distribution 'Nope' -ToolkitRoot $TestDrive } |
                Should -Throw '*does not contain the speechkit_narrate package*'
            $cfg = InModuleScope SpeechKitVoiceTtsOfflinePipeline { $script:Config }
            $cfg.Backend | Should -Be 'Auto'  # none of the settings given with the bad folder were applied
            $cfg.Python | Should -BeNullOrEmpty
            $cfg.WslPython | Should -BeNullOrEmpty
            $cfg.Distribution | Should -BeNullOrEmpty
            $cfg.ToolkitRoot | Should -Be $Root
            # the cached Python is still the one for the unchanged settings
            InModuleScope SpeechKitVoiceTtsOfflinePipeline { $script:Cache.Python } | Should -Be $before
        }

        It 'applies a valid call as a whole and then clears the caches' {
            InModuleScope SpeechKitVoiceTtsOfflinePipeline { Resolve-NarrationPython } | Out-Null  # fill the cache
            try {
                Set-NarrationBackend -Backend Native -ToolkitRoot $Root
                (Get-NarrationBackend -NoProbe).Backend | Should -Be 'Native'
                InModuleScope SpeechKitVoiceTtsOfflinePipeline { $script:Cache.Python } | Should -BeNullOrEmpty  # reset after applying
            }
            finally { Set-NarrationBackend -Backend Auto }
        }

        It 'switches to Installed mode with an empty -ToolkitRoot and back with a folder' {
            try {
                Set-NarrationBackend -ToolkitRoot ''
                $b = Get-NarrationBackend -NoProbe
                $b.Mode | Should -Be 'Installed'
                $b.ToolkitRoot | Should -BeNullOrEmpty
                # Installed mode puts nothing on PYTHONPATH, natively or in the registered entry
                $spec = InModuleScope SpeechKitVoiceTtsOfflinePipeline { Get-NarrationLaunchSpec -Arguments @('mcp') -ForRegistration }
                @($spec.Environment.Keys) | Should -Not -Contain 'PYTHONPATH'
                @($spec.Environment.Keys) | Should -Contain 'PYTHONUTF8'
            }
            finally { Set-NarrationBackend -ToolkitRoot $Root }
            (Get-NarrationBackend -NoProbe).Mode | Should -Be 'Source'
        }

        It 'explains how to install the package when Python cannot import it in Installed mode' {
            Push-Location $WorkBase  # a folder without the package, so `python -m` cannot find it in the current folder
            try {
                Set-NarrationBackend -ToolkitRoot ''
                { Get-NarrationStage } | Should -Throw '*No module named*speechkit_narrate*pip install*'
            }
            finally { Pop-Location; Set-NarrationBackend -ToolkitRoot $Root }
        }
    }

    # Python discovery: an explicit choice wins, and discovery on Windows caches its answer.
    Context 'Python discovery' {
        BeforeAll {
            $script:FakePy = New-FakePython (Join-Path $WorkBase 'fake python/python3')
        }
        AfterAll {
            $env:SPEECHKIT_PYTHON = $null
            Set-NarrationBackend -Python ''  # back to discovery, with fresh caches
        }

        # Review fix: an explicit SPEECHKIT_PYTHON (or -Python) must win over anything the module would find itself.
        It 'uses SPEECHKIT_PYTHON before any discovered Python' {
            $env:SPEECHKIT_PYTHON = $FakePy
            try {
                $r = InModuleScope SpeechKitVoiceTtsOfflinePipeline { Resolve-NarrationPython -Refresh }
                $r.Executable | Should -Be $FakePy
                $r.Version | Should -Be '3.13.0'
                # and Set-NarrationBackend -Python beats the environment variable
                Set-NarrationBackend -Python $Py
                (InModuleScope SpeechKitVoiceTtsOfflinePipeline { Resolve-NarrationPython -Refresh }).Executable | Should -Be $Py
            }
            finally { $env:SPEECHKIT_PYTHON = $null; Set-NarrationBackend -Python '' }
        }

        # Bug fix: the Windows search loop's variable was $base, the same variable as the -Base switch (names ignore
        # case), so it overwrote the switch and the Python it found was not cached. Here the module believes it runs on
        # Windows and the only working candidate sits in %LOCALAPPDATA%\Programs\Python\Python313\python.exe.
        It 'caches the Python that the Windows search finds in the install folders' {
            $local = Join-Path $WorkBase 'LocalAppData'
            $programs = Join-Path $WorkBase 'ProgramFiles'
            New-Item -ItemType Directory -Path $local, $programs -Force | Out-Null
            # On Linux, Join-Path keeps the backslashes, so this is one file name containing backslashes.
            $winPython = Join-Path $local 'Programs\Python\Python313\python.exe'
            New-FakePython $winPython | Out-Null
            $saved = @{ L = $env:LOCALAPPDATA; P = $env:ProgramFiles }
            $env:LOCALAPPDATA = $local; $env:ProgramFiles = $programs  # ProgramFiles last: the old loop left it in $Base
            try {
                $r = InModuleScope SpeechKitVoiceTtsOfflinePipeline {
                    $realWindows = $script:OnWindows
                    $script:OnWindows = $true  # take the Windows branch of the discovery
                    try {
                        Reset-NarrationCache
                        $found = Resolve-NarrationPython
                        [pscustomobject]@{ Found = $found; Cached = $script:Cache.Python; Again = Resolve-NarrationPython }
                    }
                    finally { $script:OnWindows = $realWindows; Reset-NarrationCache }
                }
                $r.Found.Executable | Should -Be $winPython
                $r.Cached | Should -Not -BeNullOrEmpty  # the answer was kept for the session
                $r.Cached.Executable | Should -Be $winPython
                [object]::ReferenceEquals($r.Again, $r.Cached) | Should -BeTrue  # the second call reused it
            }
            finally { $env:LOCALAPPDATA = $saved.L; $env:ProgramFiles = $saved.P }
        }
    }

    # Stages and tools: the registry seen from PowerShell, the generated per-tool commands and their parameter types,
    # real tool runs, and tab completion.
    Context 'Stages and tools' {
        It 'Get-NarrationStage returns the 9 built-in stages in run order with what they need' {
            $stages = @(Get-NarrationStage)
            $stages.Count | Should -Be 9
            $stages | ForEach-Object { $_.PSObject.TypeNames[0] | Should -Be 'SpeechKitVoiceTtsOfflinePipeline.Stage' }
            $stages.Name | Should -Be @('extract', 'check', 'script', 'prevoice', 'narrate', 'autofix', 'audit', 'report', 'export')
            ($stages | Where-Object Name -eq 'extract').Needs | Should -BeNullOrEmpty
            ($stages | Where-Object Name -eq 'narrate').Needs | Should -Be @('script', 'prevoice')
            ($stages | Where-Object Name -eq 'export').Needs | Should -Be @('narrate')
            ($stages | Where-Object Name -eq 'narrate').Platforms | Should -Be @('linux')
            ($stages | Where-Object Name -eq 'narrate').Where | Should -Be 'here'  # this host is Linux
            ($stages | Where-Object Name -eq 'extract').Source | Should -Be 'speechkit-narrate'
            (Get-NarrationStage -Name 'nar*').Name | Should -Be 'narrate'
        }

        It 'Get-NarrationTool lists the tools with their generated command names' {
            $tools = @(Get-NarrationTool)
            $tools.Name | Should -Be @('speak', 'quick')
            $tools.Command | Should -Be @('Invoke-NarrationSpeak', 'Invoke-NarrationQuick')
            foreach ($t in $tools) { (Get-Command $t.Command -ErrorAction Stop).Source | Should -Be 'SpeechKitVoiceTtsOfflinePipeline.Tools' }
        }

        It 'generates the tool commands with typed, validated parameters' {
            $speak = Get-Command Invoke-NarrationSpeak
            Get-Mandatory Invoke-NarrationSpeak Out | Should -BeTrue  # out is required in the schema
            Get-Mandatory Invoke-NarrationSpeak Text | Should -BeFalse
            $speak.Parameters['Workers'].ParameterType | Should -Be ([int])
            $speak.Parameters['Settings'].ParameterType | Should -Be ([hashtable])
            $speak.Parameters['TextFile'].Aliases | Should -Contain 'FullName'  # the first path takes piped files
            $quick = Get-Command Invoke-NarrationQuick
            # "input" would be $Input, an automatic variable, so it is InputPath with alias Input
            $quick.Parameters['InputPath'].Aliases | Should -Contain 'Input'
            $quick.Parameters['Targets'].ParameterType | Should -Be ([string[]])
            $quick.Parameters['AcceptDraft'].ParameterType | Should -Be ([System.Management.Automation.SwitchParameter])
            Get-Mandatory Invoke-NarrationQuick Out | Should -BeTrue
            (Get-Help Invoke-NarrationSpeak).Examples.Example[0].Code | Should -Match '^Invoke-NarrationSpeak -Text'
            (Get-Help Invoke-NarrationQuick).Synopsis | Should -Match 'Run any stage'
        }

        It 'Invoke-NarrationQuick makes a project from relative paths with spaces' {
            Push-Location $Work
            try { $r = Invoke-NarrationQuick -InputPath './small book.txt' -Out './quick out/proj' -Targets script }
            finally { Pop-Location }
            $r.PSObject.TypeNames[0] | Should -Be 'SpeechKitVoiceTtsOfflinePipeline.ToolResult'
            $r.Tool | Should -Be 'quick'
            $r.project | Should -Be (Join-Path $Work 'quick out/proj')
            $r.ran | Should -Be @('extract', 'check', 'script')
            Join-Path $Work 'quick out/proj/work/request.json' | Should -Exist
        }

        It 'Invoke-NarrationTool takes snake_case or PascalCase keys' {
            $a = Invoke-NarrationTool -Name quick -Parameters @{ input = $BookTxt; out = (Join-Path $Work 'tool/A'); targets = @('check'); accept_draft = $true }
            $a.ran | Should -Be @('extract', 'check')
            $b = Invoke-NarrationTool quick @{ Input = $BookTxt; Out = (Join-Path $Work 'tool/B'); Targets = 'extract'; AcceptDraft = $false }
            $b.ran | Should -Be @('extract')
        }

        It 'tab-completes tool names' {
            $line = 'Invoke-NarrationTool -Name sp'
            (TabExpansion2 -inputScript $line -cursorColumn $line.Length).CompletionMatches.CompletionText | Should -Be @('speak')
        }
    }

    # Project workflow: one project ($Project) taken through creation, a dry run, a real run, planning, status,
    # settings, single-stage runs, dynamic parameters, completion and pipeline binding. The It blocks run in order
    # and depend on each other.
    Context 'Project workflow' {
        It 'New-NarrationProject makes a project from a .txt file, with -Set as a hashtable' {
            $p = New-NarrationProject -Path $Project -InputFile $BookTxt -Title $Title -Author 'A. Writer' -Set @{ spoken = @{ years = $true } }
            $p.PSObject.TypeNames[0] | Should -Be 'SpeechKitVoiceTtsOfflinePipeline.ProjectInfo'
            $p.Project | Should -Be $Project
            $p.Title | Should -BeExactly $Title
            $p.Settings.spoken.years | Should -BeTrue
            Join-Path $Project 'project.json' | Should -Exist
            Join-Path $Project 'input/small book.txt' | Should -Exist
            (Get-Content -Raw (Join-Path $Project 'project.json') | ConvertFrom-Json).spoken.years | Should -BeTrue
        }

        It 'New-NarrationProject takes -Set as JSON text and refuses JSON that is not an object' {
            $dir = Join-Path $Work 'json set'
            $p = New-NarrationProject $dir $BookTxt -Set '{"export": {"format": "mp3"}, "spoken": {"code_names": true}}'
            $p.Settings.export.format | Should -Be 'mp3'
            $p.Settings.spoken.code_names | Should -BeTrue
            { New-NarrationProject (Join-Path $Work 'bad set') $BookTxt -Set '[1, 2]' } | Should -Throw '*-Set must be a hashtable or a JSON object*'
            Join-Path $Work 'bad set' | Should -Not -Exist  # refused before Python ran
        }

        It 'refuses a missing input file before calling Python' {
            { New-NarrationProject -Path (Join-Path $Work 'nope') -InputFile (Join-Path $Work 'missing.txt') } | Should -Throw '*does not exist*'
            New-NarrationProject -Path (Join-Path $Work 'whatif') -InputFile $BookTxt -WhatIf | Should -BeNullOrEmpty
            Join-Path $Work 'whatif' | Should -Not -Exist
        }

        It '-WhatIf shows the plan and runs nothing' {
            Invoke-NarrationPipeline -Path $Project -Target script -AcceptDraft -WhatIf | Should -BeNullOrEmpty
            Join-Path $Project 'work/draft.md' | Should -Not -Exist
            Join-Path $Project $StateFile | Should -Not -Exist
            (Get-NarrationPlan $Project -Target script | Where-Object Stage -eq 'script').Action | Should -Be 'run'
        }

        It 'Invoke-NarrationPipeline -Target script -AcceptDraft builds the narration script' {
            $r = [pscustomobject]@{ Project = $Project } | Invoke-NarrationPipeline -Target script -AcceptDraft
            $r.PSObject.TypeNames[0] | Should -Be 'SpeechKitVoiceTtsOfflinePipeline.RunResult'
            $r.Ran | Should -Be @('extract', 'check', 'script')
            $r.Stopped | Should -BeNullOrEmpty
            $r.Steps[0].PSObject.TypeNames[0] | Should -Be 'SpeechKitVoiceTtsOfflinePipeline.RunStep'
            Join-Path $Project 'work/request.json' | Should -Exist
            Join-Path $Project 'work/book.md' | Should -Exist
        }

        It 'Get-NarrationPlan afterwards shows script as skip (up to date)' {
            $plan = @(Get-NarrationPlan -Path $Project -Target script)
            $plan[0].PSObject.TypeNames[0] | Should -Be 'SpeechKitVoiceTtsOfflinePipeline.PlanStep'
            $script = $plan | Where-Object Stage -eq 'script'
            $script.Action | Should -Be 'skip'
            $script.Reasons | Should -Be @('up to date')
        }

        It 'Get-NarrationProject shows the stages that are done and the narration still to voice' {
            $p = Get-NarrationProject $Project
            $p.PSObject.TypeNames[0] | Should -Be 'SpeechKitVoiceTtsOfflinePipeline.Project'
            $p.Title | Should -BeExactly $Title
            $p.Done | Should -Be @('extract', 'check', 'script')
            ($p.Stages | Where-Object Stage -eq 'narrate').Missing | Should -Contain 'speech'
            $p.Narration.segments | Should -BeGreaterThan 0
            $p.Narration.voiced | Should -Be 0
            $p.Exported | Should -BeNullOrEmpty
        }

        It 'Set-NarrationSetting deep-merges, and the plan then shows script as run' {
            Set-NarrationSetting -Path $Project -Settings @{ spoken = @{ dates = 'dmy' } }
            $s = Get-NarrationSetting $Project
            $s.spoken.dates | Should -Be 'dmy'
            $s.spoken.years | Should -BeTrue  # the earlier key in the same section stays
            $s.title | Should -BeExactly $Title
            (Get-NarrationSetting $Project -Effective).export.format | Should -Be 'wav'  # a default filled in
            $script = Get-NarrationPlan $Project -Target script | Where-Object Stage -eq 'script'
            $script.Action | Should -Be 'run'
            $script.Reasons | Should -Contain 'setting spoken changed'
        }

        It '-WhatIf on a changed project still runs nothing' {
            $state = Join-Path $Project $StateFile
            $before = (Get-Item $state).LastWriteTimeUtc
            Invoke-Narration $Project -Target script -WhatIf | Should -BeNullOrEmpty
            (Get-Item $state).LastWriteTimeUtc | Should -Be $before
            (Get-NarrationPlan $Project -Target script | Where-Object Stage -eq 'script').Action | Should -Be 'run'
        }

        It 'Invoke-NarrationStage runs the stage and brings it up to date' {
            $r = Invoke-NarrationStage -Path $Project -Name script
            $r.PSObject.TypeNames[0] | Should -Be 'SpeechKitVoiceTtsOfflinePipeline.StageResult'
            $r.Action | Should -Be 'ran'
            $r.Ran | Should -Be @('script')
            (Get-NarrationPlan $Project -Target script | Where-Object Stage -eq 'script').Action | Should -Be 'skip'
        }

        It 'Invoke-NarrationStage runs stale prerequisites first (like make); -Only runs just the stage' {
            $fresh = Join-Path $Work 'make like'
            New-NarrationProject -Path $fresh -InputFile $BookTxt | Out-Null
            (Invoke-NarrationStage -Path $fresh -Name script -AcceptDraft).Ran | Should -Be @('extract', 'check', 'script')
            (Invoke-NarrationStage -Path $fresh -Name script -Only).Ran | Should -Be @('script')
        }

        It 'offers each stage''s options as dynamic parameters' {
            $narrate = Get-Command Invoke-NarrationStage -ArgumentList $Project, 'narrate'
            $narrate.Parameters['Workers'].ParameterType | Should -Be ([int])
            ($narrate.Parameters['Workers'].Attributes | Where-Object { $_ -is [System.Management.Automation.ValidateRangeAttribute] }).MinRange | Should -Be 1
            $autofix = Get-Command Invoke-NarrationStage -ArgumentList $Project, 'autofix'
            $autofix.Parameters.Keys | Should -Contain 'Takes'
            $autofix.Parameters.Keys | Should -Contain 'Retakes'
            $autofix.Parameters['Takes'].ParameterType | Should -Be ([int])
        }

        It 'tab-completes stage names for -Name, -Target and -Force' {
            $line = "Invoke-NarrationStage -Path '$Project' -Name a"
            (TabExpansion2 -inputScript $line -cursorColumn $line.Length).CompletionMatches.CompletionText | Should -Be @('autofix', 'audit')
            $line = "Invoke-NarrationPipeline '$Project' -Force a"
            (TabExpansion2 -inputScript $line -cursorColumn $line.Length).CompletionMatches.CompletionText | Should -Be @('autofix', 'audit', 'all')
        }

        It 'binds pipeline input by the Project property' {
            @(Get-NarrationProject $Project | Get-NarrationPlan -Target script).Stage | Should -Be @('extract', 'check', 'script')
        }
    }

    # Plug-ins: SPEECHKIT_PLUGINS set to the test plug-in for this Context only.
    Context 'Plug-ins' {
        BeforeAll { $env:SPEECHKIT_PLUGINS = $StatsPlugin }
        AfterAll {
            $env:SPEECHKIT_PLUGINS = $null
            Update-NarrationCommand  # regenerate the tool commands without the plug-in
        }

        It 'Update-NarrationCommand creates Invoke-NarrationWordcount from SPEECHKIT_PLUGINS' {
            Get-Command Invoke-NarrationWordcount -ErrorAction SilentlyContinue | Should -BeNullOrEmpty
            $gen = @(Update-NarrationCommand -PassThru)
            $gen[0].PSObject.TypeNames[0] | Should -Be 'SpeechKitVoiceTtsOfflinePipeline.GeneratedCommand'
            $gen.Name | Should -Contain 'Invoke-NarrationWordcount'
            Get-Mandatory Invoke-NarrationWordcount File | Should -BeTrue
            Get-Command Invoke-NarrationSpeak | Should -Not -BeNullOrEmpty  # built-in commands survive
            (Invoke-NarrationWordcount -File $BookTxt).words | Should -BeGreaterThan 50
            (Get-Item -LiteralPath $BookTxt | Invoke-NarrationWordcount).words | Should -BeGreaterThan 50  # piped FileInfo
        }

        It 'Get-NarrationStage includes the plug-in stage stats' {
            $stats = Get-NarrationStage -Name stats
            $stats.Needs | Should -Be @('script')
            $stats.Source | Should -Be $StatsPlugin
            @(Get-NarrationStage).Count | Should -Be 10
        }

        It 'runs the plug-in stage with its option as a dynamic parameter' {
            $r = Invoke-NarrationStage $Project stats -Wpm 120
            $r.Action | Should -Be 'ran'
            $r.Result.wpm | Should -Be 120
            (Get-Content -Raw (Join-Path $Project 'out/stats.json') | ConvertFrom-Json).reading_minutes | Should -BeGreaterThan 0
        }

        It 'removes the plug-in command again once the plug-in is gone' {
            $env:SPEECHKIT_PLUGINS = $null
            Update-NarrationCommand
            Get-Command Invoke-NarrationWordcount -ErrorAction SilentlyContinue | Should -BeNullOrEmpty
        }
    }

    # MCP registration: Register-NarrationMcpServer merging into a client config, and both ways of starting a server.
    Context 'MCP registration' {
        BeforeEach {
            $script:Cfg = Join-Path $TestDrive "mcp $([guid]::NewGuid().ToString('n')).json"
            $original = '{ "mcpServers": { "other": { "command": "node", "args": ["server.js"], "env": { "A": "1" } } }, "theme": "dark", "list": [1] }'
            [System.IO.File]::WriteAllText($Cfg, $original)
        }

        It 'keeps the other entries and adds a valid speechkit-voice-tts-offline-pipeline entry, with a backup' {
            $r = Register-NarrationMcpServer -ConfigPath $Cfg
            $r.PSObject.TypeNames[0] | Should -Be 'SpeechKitVoiceTtsOfflinePipeline.McpRegistration'
            $json = Get-Content -Raw -LiteralPath $Cfg | ConvertFrom-Json
            $json.theme | Should -Be 'dark'
            @($json.list) | Should -Be @(1)
            $json.mcpServers.other.command | Should -Be 'node'
            @($json.mcpServers.other.args) | Should -Be @('server.js')
            $b = $json.mcpServers.'speechkit-voice-tts-offline-pipeline'
            $b.command | Should -Exist
            @($b.args) | Should -Be @('-m', 'speechkit_narrate', '--json', 'mcp')
            $b.env.PYTHONPATH | Should -Be $Root
            $b.env.PYTHONUTF8 | Should -Be '1'
            $r.Backup | Should -Exist
            [System.IO.File]::ReadAllBytes($Cfg)[0] | Should -Be ([byte][char]'{')  # no BOM
            "$Cfg.tmp" | Should -Not -Exist
        }

        It 'migrates an identified legacy default with an exact backup' {
            Register-NarrationMcpServer -ConfigPath $Cfg -Name 'speechkit-narrate' | Out-Null
            $before = [System.IO.File]::ReadAllText($Cfg)
            $result = Register-NarrationMcpServer -ConfigPath $Cfg
            $after = Get-Content -Raw -LiteralPath $Cfg | ConvertFrom-Json
            @($after.mcpServers.PSObject.Properties.Name) | Should -Not -Contain 'speechkit-narrate'
            @($after.mcpServers.PSObject.Properties.Name) | Should -Contain 'speechkit-voice-tts-offline-pipeline'
            $after.mcpServers.other.command | Should -Be 'node'
            [System.IO.File]::ReadAllText($result.Backup) | Should -BeExactly $before
        }

        It 'leaves ambiguous canonical and legacy entries unchanged' {
            Register-NarrationMcpServer -ConfigPath $Cfg | Out-Null
            Register-NarrationMcpServer -ConfigPath $Cfg -Name 'speechkit-narrate' | Out-Null
            $before = [System.IO.File]::ReadAllText($Cfg)
            { Register-NarrationMcpServer -ConfigPath $Cfg } | Should -Throw '*Both canonical and legacy*'
            [System.IO.File]::ReadAllText($Cfg) | Should -BeExactly $before
        }

        It 'does not replace an unidentified legacy registration' {
            $original = '{"mcpServers":{"speechkit-narrate":{"command":"node","args":["unrelated.js"]}}}'
            [System.IO.File]::WriteAllText($Cfg, $original)
            { Register-NarrationMcpServer -ConfigPath $Cfg } | Should -Throw '*not an identified*'
            [System.IO.File]::ReadAllText($Cfg) | Should -BeExactly $original
        }

        It 'writes SPEECHKIT_DELEGATE and a custom name' {
            Register-NarrationMcpServer -ConfigPath $Cfg -Name 'narrate-dev' -Delegate 'wsl.exe -d Ubuntu-24.04 --exec' -NoBackup | Out-Null
            $json = Get-Content -Raw -LiteralPath $Cfg | ConvertFrom-Json
            $json.mcpServers.'narrate-dev'.env.SPEECHKIT_DELEGATE | Should -Be 'wsl.exe -d Ubuntu-24.04 --exec'
            @(Get-ChildItem -LiteralPath $TestDrive -Filter "$(Split-Path -Leaf $Cfg).*.bak").Count | Should -Be 0
        }

        It '-WhatIf leaves the file alone; a new file and its folder are created' {
            $before = [System.IO.File]::ReadAllText($Cfg)
            Register-NarrationMcpServer -ConfigPath $Cfg -WhatIf | Should -BeNullOrEmpty
            [System.IO.File]::ReadAllText($Cfg) | Should -BeExactly $before
            $fresh = Join-Path $TestDrive 'new folder/claude_desktop_config.json'
            Register-NarrationMcpServer -ConfigPath $fresh | Out-Null
            (Get-Content -Raw $fresh | ConvertFrom-Json).mcpServers.'speechkit-voice-tts-offline-pipeline'.env.PYTHONPATH | Should -Be $Root
        }

        # Bug fix: the temporary <file>.tmp used to stay behind when the final copy failed. Here the "config file" is
        # a folder, so writing the .tmp works and copying it over the target fails.
        It 'removes its temporary file when the final copy fails' {
            $dirAsFile = Join-Path $TestDrive 'config is a folder'
            New-Item -ItemType Directory -Path $dirAsFile | Out-Null
            { Register-NarrationMcpServer -ConfigPath $dirAsFile -NoBackup } | Should -Throw
            "$dirAsFile.tmp" | Should -Not -Exist
            Test-Path -LiteralPath $dirAsFile -PathType Container | Should -BeTrue  # the folder itself is untouched
        }

        It 'writes an entry that starts a working MCP server' {
            Register-NarrationMcpServer -ConfigPath $Cfg -NoBackup | Out-Null
            $b = (Get-Content -Raw -LiteralPath $Cfg | ConvertFrom-Json).mcpServers.'speechkit-voice-tts-offline-pipeline'
            $envMap = @{}
            foreach ($p in $b.env.PSObject.Properties) { $envMap[$p.Name] = $p.Value }
            $reply = Invoke-McpInitialize -FilePath $b.command -Arguments @($b.args) -Environment $envMap
            $reply.id | Should -Be 1
            $reply.result.serverInfo.name | Should -Not -BeNullOrEmpty
        }

        It 'Start-NarrationMcpServer serves MCP on the console''s stdin/stdout' {
            $pwsh = (Get-Process -Id $PID).Path
            $reply = Invoke-McpInitialize -FilePath $pwsh -Arguments @('-NoProfile', '-NonInteractive', '-Command',
                "Import-Module '$Manifest'; Start-NarrationMcpServer")
            $reply.id | Should -Be 1
            $reply.result.capabilities.tools | Should -Not -BeNullOrEmpty
        }

        It 'Start-NarrationMcpServer -WhatIf shows the command line and starts no server' {
            $out = Invoke-ChildPwsh 'Start-NarrationMcpServer -WhatIf'
            $out | Should -Match 'What if: Performing the operation'
            $out | Should -Match '-m speechkit_narrate'
            $out | Should -Match 'mcp'
        }

        It 'refuses to overwrite a file that is not valid JSON' {
            [System.IO.File]::WriteAllText($Cfg, '{ "mcpServers": ')
            { Register-NarrationMcpServer -ConfigPath $Cfg } | Should -Throw '*not valid JSON*'
            [System.IO.File]::ReadAllText($Cfg) | Should -BeExactly '{ "mcpServers": '
        }

        It 'refuses to register in Installed mode when the Python cannot import the package' {
            Push-Location $WorkBase  # a folder without the package
            try {
                Set-NarrationBackend -ToolkitRoot ''
                $target = Join-Path $TestDrive 'refused.json'
                { Register-NarrationMcpServer -ConfigPath $target } | Should -Throw '*Nothing was written*pip install*'
                $target | Should -Not -Exist
            }
            finally { Pop-Location; Set-NarrationBackend -ToolkitRoot $Root }
        }
    }

    # WSL backend: the Wsl backend with fake_wsl.py standing in for wsl.exe. Checks the launcher prefix, path
    # translation, the env/python command line, and that paths in results map back.
    Context 'WSL backend (fake wsl.exe)' {
        BeforeAll {
            $env:SPEECHKIT_WSL = "`"$Py`" `"$FakeWsl`""  # both paths quoted, so spaces survive the split
            $env:FAKE_WSL_LOG = Join-Path $TestDrive 'fake_wsl.log'  # fake_wsl.py appends every forwarded command line
            # /proc/self/root/<path> is <path> on Linux, so the fake wslpath changes every path and it still opens
            $script:WslRoot = '/proc/self/root'
        }
        AfterAll {
            foreach ($k in @('SPEECHKIT_WSL', 'FAKE_WSL_LOG', 'FAKE_WSL_PREFIX', 'SPEECHKIT_PLUGINS')) { Remove-Item -LiteralPath "Env:$k" -ErrorAction SilentlyContinue }
            Set-NarrationBackend -Backend Auto -WslPython '' -Distribution ''
        }

        It 'builds the default wsl.exe --exec prefix for a distribution' {
            $saved = $env:SPEECHKIT_WSL
            try {
                $env:SPEECHKIT_WSL = $null
                Set-NarrationBackend -Backend Wsl -Distribution 'Ubuntu-24.04'
                $prefix = InModuleScope SpeechKitVoiceTtsOfflinePipeline { Get-NarrationWslPrefix }
                Split-Path -Leaf $prefix.FilePath | Should -Be 'wsl.exe'
                $prefix.Arguments | Should -Be @('-d', 'Ubuntu-24.04', '--exec')
            }
            finally { $env:SPEECHKIT_WSL = $saved; Set-NarrationBackend -Distribution '' }
        }

        It 'translates paths with wslpath and builds env PYTHONPATH=... python3 -m speechkit_narrate --json' {
            $env:FAKE_WSL_PREFIX = '/wsl'
            $env:SPEECHKIT_PLUGINS = $StatsPlugin
            Set-NarrationBackend -Backend Wsl -WslPython python3
            try {
                InModuleScope SpeechKitVoiceTtsOfflinePipeline { ConvertTo-NarrationWslPath -Path '/tmp/a b/c.txt' } | Should -Be '/wsl/tmp/a b/c.txt'
                $spec = InModuleScope SpeechKitVoiceTtsOfflinePipeline { Get-NarrationLaunchSpec -Arguments @('plan', '/x', '--options={"a": 1}') }
                $spec.FilePath | Should -Be $Py
                $spec.ArgumentList | Should -Be @($FakeWsl, 'env', "PYTHONPATH=/wsl$Root", 'PYTHONUTF8=1', 'PYTHONIOENCODING=utf-8',
                    "SPEECHKIT_PLUGINS=/wsl$StatsPlugin", 'python3', '-m', 'speechkit_narrate', '--json', 'plan', '/x', '--options={"a": 1}')
            }
            finally { $env:FAKE_WSL_PREFIX = $null; $env:SPEECHKIT_PLUGINS = $null }
        }

        It 'runs Get-NarrationStage through WSL' {
            Remove-Item -LiteralPath $env:FAKE_WSL_LOG -ErrorAction SilentlyContinue
            Set-NarrationBackend -Backend Wsl -WslPython ''  # the module must look for the WSL python itself
            @(Get-NarrationStage).Count | Should -Be 9
            (Get-NarrationBackend).WslPython | Should -Be 'python3'  # no venv python here, so python3
            $log = Get-Content -LiteralPath $env:FAKE_WSL_LOG
            $log | Should -Contain 'test -x /opt/speechkit-narrate/venv/bin/python'  # the venv made by Install -Wsl
            $log | Should -Contain "env PYTHONPATH=$Root PYTHONUTF8=1 PYTHONIOENCODING=utf-8 python3 -m speechkit_narrate --json stages"
        }

        It 'converts project and tool paths for WSL and maps the results back' {
            $env:FAKE_WSL_PREFIX = $WslRoot
            Remove-Item -LiteralPath $env:FAKE_WSL_LOG -ErrorAction SilentlyContinue
            Set-NarrationBackend -Backend Wsl -WslPython python3
            try {
                (@(Get-NarrationPlan -Path $Project -Target script) | Where-Object Stage -eq 'script').Action | Should -Be 'skip'
                $out = Join-Path $Work 'wsl quick'
                $r = Invoke-NarrationQuick -InputPath $BookTxt -Out $out -Targets extract
                $r.project | Should -Not -Match 'proc/self/root'  # the prefix was mapped back off
                $r.project | Should -Be $out
                Join-Path $out 'work/draft.md' | Should -Exist
                $log = Get-Content -LiteralPath $env:FAKE_WSL_LOG -Raw
                $log | Should -Match ([regex]::Escape("--json plan $WslRoot$Project script"))
                $log | Should -Match ([regex]::Escape("`"out`":`"$WslRoot$out`""))  # the path inside the JSON
            }
            finally { $env:FAKE_WSL_PREFIX = $null }
        }

        It 'passes JSON stage options through WSL unchanged' {
            Set-NarrationBackend -Backend Wsl -WslPython python3
            $r = Invoke-NarrationStage -Path $Project -Name script -Option @{ note = 'say "hi" & $HOME (1)' }
            $r.Action | Should -Be 'ran'
            Get-Content -LiteralPath $env:FAKE_WSL_LOG -Raw | Should -Match ([regex]::Escape('--options={"script":{"note":"say \"hi\" & $HOME (1)"}}'))
        }
    }

    # Auto backend on Windows (simulated): the module believes it runs on Windows and the toolkit believes the same,
    # so Linux-only stages and tools go through SPEECHKIT_DELEGATE, here fake_wsl.py.
    Context 'Auto backend on Windows (simulated)' {
        BeforeAll {
            $script:RealOnWindows = InModuleScope SpeechKitVoiceTtsOfflinePipeline { $script:OnWindows }
            InModuleScope SpeechKitVoiceTtsOfflinePipeline { $script:OnWindows = $true }
            $env:SPEECHKIT_WSL = "`"$Py`" `"$FakeWsl`""
            $env:FAKE_WSL_LOG = Join-Path $TestDrive 'fake_wsl_auto.log'
            $env:SPEECHKIT_PYTHON = $Py  # on "Windows" discovery would look for py -3.13; use the real Python
            $env:SPEECHKIT_PLATFORM = 'windows'  # the Python side treats this host as Windows
            # PowerShell 7.5 can retain an empty environment value; Python must still choose its default.
            $env:SPEECHKIT_DELEGATE_PYTHON = ''
            Set-NarrationBackend -Backend Auto -WslPython python3
            $script:Delegated = Join-Path $Work 'delegated'
            New-NarrationProject -Path $Delegated -InputFile $BookTxt | Out-Null
            New-Item -ItemType Directory -Path (Join-Path $Delegated 'plugins') | Out-Null
            # A project plug-in: stage "penguin" needs script, is Linux-only and writes sys.platform to out/penguin.txt.
            $plugin = @(
                'import sys'
                'from speechkit_narrate.registry import stage'
                ''
                ''
                '@stage("penguin", needs=["script"], outputs=["out/penguin.txt"], platforms=["linux"], description="Linux-only test stage")'
                'def penguin(project, ctx):'
                '    (project.root / "out").mkdir(exist_ok=True)'
                '    (project.root / "out" / "penguin.txt").write_text(sys.platform, encoding="utf-8")'
                '    return {"platform": sys.platform}'
            ) -join "`n"
            [System.IO.File]::WriteAllText((Join-Path $Delegated 'plugins/penguin.py'), $plugin + "`n")
        }
        AfterAll {
            InModuleScope SpeechKitVoiceTtsOfflinePipeline -Parameters @{ Value = $RealOnWindows } { param($Value) $script:OnWindows = $Value }
            foreach ($k in @('SPEECHKIT_WSL', 'FAKE_WSL_LOG', 'SPEECHKIT_PYTHON', 'SPEECHKIT_PLATFORM', 'SPEECHKIT_DELEGATE_PYTHON')) { Remove-Item -LiteralPath "Env:$k" -ErrorAction SilentlyContinue }
            Set-NarrationBackend -Backend Auto -WslPython ''
        }

        It 'sets SPEECHKIT_DELEGATE for the Python side' {
            $b = Get-NarrationBackend
            $b.Platform | Should -Be 'Windows'
            $b.Delegate | Should -Be "$Py $FakeWsl"
            $spec = InModuleScope SpeechKitVoiceTtsOfflinePipeline { Get-NarrationLaunchSpec -Arguments @('stages') }
            $spec.Environment['SPEECHKIT_DELEGATE'] | Should -Be "$Py $FakeWsl"
            (Get-NarrationStage -Name narrate).Where | Should -Be 'delegate'
        }

        It 'plans a Linux-only stage for delegation and runs it through the WSL command' {
            $step = Get-NarrationPlan $Delegated -Target penguin | Where-Object Stage -eq 'penguin'
            $step.Where | Should -Be 'delegate'
            $step.Action | Should -Be 'run'
            $r = Invoke-NarrationPipeline $Delegated -Target penguin -AcceptDraft
            $r.Ran | Should -Contain 'penguin'
            Get-Content -LiteralPath (Join-Path $Delegated 'out/penguin.txt') | Should -Be 'linux'
            Get-Content -LiteralPath $env:FAKE_WSL_LOG -Raw | Should -Match '--only penguin --force penguin'
        }

        It 'reports the stage as blocked with the Native backend' {
            Set-NarrationBackend -Backend Native
            try { (Get-NarrationPlan $Delegated -Force penguin -Only penguin).Action | Should -Be 'blocked' }
            finally { Set-NarrationBackend -Backend Auto }
        }

        It 'leaves a Linux-only tool to the toolkit, which delegates it (paths and files round-trip)' {
            $toolFile = Join-Path $Work 'tux_tool.py'
            $code = @(
                'import sys'
                'from speechkit_narrate.registry import tool'
                ''
                ''
                '@tool("tux", params={"file": {"type": "string", "format": "path"}}, required=["file"], platforms=["linux"],'
                '      description="Linux-only test tool")'
                'def tux(file, log=print):'
                '    return {"files": [file], "platform": sys.platform}'
            ) -join "`n"
            [System.IO.File]::WriteAllText($toolFile, $code + "`n")
            $env:SPEECHKIT_PLUGINS = $toolFile
            try {
                Update-NarrationCommand
                (Get-NarrationTool tux).RunsHere | Should -BeFalse
                Push-Location $Work
                try { $r = Invoke-NarrationTux -File './small book.txt' } finally { Pop-Location }
                $r.platform | Should -Be 'linux'
                $r.Files | Should -Be @($BookTxt)
                Get-Content -LiteralPath $env:FAKE_WSL_LOG -Raw | Should -Match ([regex]::Escape('--json tool tux --params'))
            }
            finally {
                $env:SPEECHKIT_PLUGINS = $null
                Update-NarrationCommand
            }
        }

        It 'Register-NarrationMcpServer writes the delegation command' {
            $cfg = Join-Path $TestDrive 'auto_mcp.json'
            Register-NarrationMcpServer -ConfigPath $cfg | Out-Null
            (Get-Content -Raw $cfg | ConvertFrom-Json).mcpServers.'speechkit-voice-tts-offline-pipeline'.env.SPEECHKIT_DELEGATE | Should -Be "$Py $FakeWsl"
        }
    }

    # Errors: how failures from Python and the module become PowerShell errors.
    Context 'Errors' {
        It 'throws the Python error text for a missing project' {
            $missing = Join-Path $Work 'no such project'
            $err = $null
            try { Get-NarrationProject -Path $missing } catch { $err = $_ }
            $err | Should -Not -BeNullOrEmpty
            $err.Exception.Message | Should -BeLike '*project.json not found*'
            $err.FullyQualifiedErrorId | Should -Be 'SpeechKitVoiceTtsOfflinePipeline.ProjectError,Get-NarrationProject'
            $err.CategoryInfo.Category | Should -Be 'ObjectNotFound'
            $err.Exception.Data['NarrationType'] | Should -Be 'ProjectError'
            { Invoke-NarrationPipeline -Path $missing } | Should -Throw '*project.json not found*'
        }

        It 'throws for an unknown stage or tool' {
            { Get-NarrationPlan -Path $Project -Target nosuchstage } | Should -Throw "*unknown stage 'nosuchstage'*"
            { Invoke-NarrationTool -Name nosuchtool } | Should -Throw "*no tool 'nosuchtool'*"
        }

        It 'reports a usage mistake with the message Python gives (as JSON)' {
            { InModuleScope SpeechKitVoiceTtsOfflinePipeline { Invoke-NarrationCli -Arguments @('--no-such-flag') } } | Should -Throw '*speechkit-voice-tts-offline-pipeline:*arguments*'
            { Invoke-NarrationTool -Name quick -Parameters @{ out = (Join-Path $Work 'q bad'); no_such = 1 } } | Should -Throw '*unexpected keyword*'
        }

        It 'reports a Python failure that printed no JSON (exit code and stderr)' {
            $fake = New-FakePython (Join-Path $TestDrive 'fakepy')
            InModuleScope SpeechKitVoiceTtsOfflinePipeline -Parameters @{ Fake = $fake } {
                param($Fake)
                $saved = $script:Config.Python
                Set-NarrationBackend -Python $Fake
                try { { Invoke-NarrationCli -Arguments @('stages') } | Should -Throw '*exited with code 2 without a result*usage: fake python broken here*' }
                finally { Set-NarrationBackend -Python $saved }
            }
        }
    }

    # Environment and installation: doctor results as objects, and the install plan.
    Context 'Environment and installation' {
        It 'Test-NarrationEnvironment returns checks (doctor) as objects' {
            $checks = @(Test-NarrationEnvironment)
            $checks[0].PSObject.TypeNames[0] | Should -Be 'SpeechKitVoiceTtsOfflinePipeline.Check'
            ($checks | Where-Object Name -eq 'python').Ok | Should -BeTrue
            ($checks | Where-Object Name -eq 'ffmpeg') | Should -Not -BeNullOrEmpty
            ($checks | Where-Object Name -eq 'speechkit checkout').Detail | Should -Be $Root
            Test-NarrationEnvironment -Quiet | Should -BeOfType ([bool])
        }

        It 'plans the winget, pip and WSL steps on Windows' {
            $steps = InModuleScope SpeechKitVoiceTtsOfflinePipeline { Get-NarrationInstallStep -Platform Windows -Wsl -Distribution 'Ubuntu-24.04' }
            $steps.Component | Select-Object -Unique | Should -Be @('Winget', 'Pip', 'WslInstall', 'WslPackages', 'WslSetup')
            $all = ($steps | ForEach-Object { $_.CommandLine }) -join "`n"
            foreach ($id in @('Python.Python.3.13', 'Gyan.FFmpeg')) { $all | Should -Match ([regex]::Escape("winget install --id $id --exact")) }
            @($steps | Where-Object Component -eq 'Winget').Count | Should -Be 2  # nothing for the removed features
            $all | Should -Match ([regex]::Escape("-m pip install --upgrade -r $(Join-Path $Root 'requirements.txt')"))
            $all | Should -Match ([regex]::Escape("-m pip install --upgrade -r '<toolkit-in-wsl>/requirements-voice.txt'"))
            Join-Path $Root 'requirements.txt' | Should -Exist
            Join-Path $Root 'requirements-voice.txt' | Should -Exist
            $all | Should -Match ([regex]::Escape('--install -d Ubuntu-24.04'))
            $all | Should -Match 'ppa:deadsnakes/ppa'
            $all | Should -Match 'python3.13-venv'
            $all | Should -Match ([regex]::Escape('/opt/speechkit-narrate/venv/bin/python -m speechkit_narrate setup'))
            $all | Should -Match ([regex]::Escape("Set-NarrationBackend -Distribution 'Ubuntu-24.04' -WslPython '/opt/speechkit-narrate/venv/bin/python'"))
        }

        # Review fix: the WslSetup text named `python3 -m bookcast setup` but the venv python ran; the Setup text said
        # Linux although it is also the default on macOS. The descriptions now say what really runs, and where.
        It 'describes the setup steps as they really run' {
            $win = InModuleScope SpeechKitVoiceTtsOfflinePipeline { Get-NarrationInstallStep -Platform Windows -Component WslSetup }
            $setup = $win | Where-Object Name -eq 'wsl speechkit-voice-tts-offline-pipeline setup'
            $setup.Description | Should -Match ([regex]::Escape('/opt/speechkit-narrate/venv/bin/python -m speechkit_narrate setup'))
            $setup.ArgumentList | Should -Contain '/opt/speechkit-narrate/venv/bin/python'
            $setup.Description | Should -Not -Match 'python3 '
            $mac = InModuleScope SpeechKitVoiceTtsOfflinePipeline { Get-NarrationInstallStep -Platform MacOS }
            @($mac).Count | Should -Be 1
            $mac[0].Component | Should -Be 'Setup'
            $mac[0].Description | Should -Match 'on Linux the voice runtime'  # not "(Linux)" for a macOS default
            $mac[0].Environment['PYTHONPATH'] | Should -Be $Root
        }

        # Review fix: the comment promised Status 'skipped' but declined and -WhatIf steps were never emitted.
        It 'Install-NarrationDependency -WhatIf runs nothing and reports every step as skipped' {
            $out = @(Install-NarrationDependency -Wsl -WhatIf)
            # Setup (the Linux default), WSL install, WSL packages, WSL setup and the final Set-NarrationBackend step
            $out.Step | Should -Be @('speechkit-voice-tts-offline-pipeline setup', 'wsl install Ubuntu-24.04', 'wsl packages', 'wsl speechkit-voice-tts-offline-pipeline setup', 'use wsl')
            foreach ($o in $out) {
                $o.PSObject.TypeNames[0] | Should -Be 'SpeechKitVoiceTtsOfflinePipeline.InstallStep'
                $o.Status | Should -BeExactly 'skipped'
                $o.ExitCode | Should -BeNullOrEmpty  # nothing ran
                $o.Command | Should -Not -BeNullOrEmpty  # the command it would have run
            }
            (Get-NarrationBackend -NoProbe).Distribution | Should -BeNullOrEmpty  # the 'use wsl' script did not run
        }

        It 'refuses to plan pip packages in Installed mode and plans setup without PYTHONPATH' {
            try {
                Set-NarrationBackend -ToolkitRoot ''
                { InModuleScope SpeechKitVoiceTtsOfflinePipeline { Get-NarrationInstallStep -Platform Windows } } | Should -Throw '*Installed mode*'
                $setup = InModuleScope SpeechKitVoiceTtsOfflinePipeline { Get-NarrationInstallStep -Platform Linux }
                $setup[0].Environment.ContainsKey('PYTHONPATH') | Should -BeFalse
            }
            finally { Set-NarrationBackend -ToolkitRoot $Root }
        }
    }
}
