# SPDX-License-Identifier: MIT
# File: tests/distribution/powershell_contract.Tests.ps1
# Purpose: Enforce Windows launcher parsing, translation, authority, and secrecy

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$WarningPreference = 'Stop'

$script:Passed = 0
$script:Failed = 0

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw "assertion failed: $Message" }
}

function Assert-Equal {
    param($Actual, $Expected, [string]$Message)
    $actualValues = @($Actual)
    $expectedValues = @($Expected)
    if ($actualValues.Count -ne $expectedValues.Count) {
        throw "assertion failed: $Message (count $($actualValues.Count), expected $($expectedValues.Count))"
    }
    for ($index = 0; $index -lt $actualValues.Count; $index++) {
        if ($actualValues[$index] -cne $expectedValues[$index]) {
            throw "assertion failed: $Message (index $index was '$($actualValues[$index])', expected '$($expectedValues[$index])')"
        }
    }
}

function Assert-Throws {
    param([scriptblock]$Action, [string]$Pattern, [string]$Message)
    $caught = $null
    $priorErrorCount = $Error.Count
    try { & $Action } catch { $caught = $_ }
    if ($null -eq $caught) { throw "assertion failed: $Message (did not throw)" }
    if ($caught.Exception.Message -notmatch $Pattern) {
        throw "assertion failed: $Message ('$($caught.Exception.Message)' did not match '$Pattern')"
    }
    $addedErrorCount = $Error.Count - $priorErrorCount
    if ($addedErrorCount -lt 1) {
        throw "assertion failed: $Message (expected an error record for the caught exception)"
    }
    for ($index = 0; $index -lt $addedErrorCount; $index++) { $Error.RemoveAt(0) }
}

function Invoke-Contract {
    param([string]$Name, [scriptblock]$Body)
    try {
        & $Body
        $script:Passed++
        Write-Output "PASS $Name"
    } catch {
        $script:Failed++
        [Console]::Error.WriteLine("FAIL ${Name}: $($_.Exception.Message)")
        $Error.Clear()
    }
}

$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
$launcher = Join-Path $root 'scripts/goalrouter.ps1'
$shim = Join-Path $root 'scripts/goalrouter.cmd'
$fakePath = Join-Path $root 'tests/fixtures/distribution/fake-wsl.ps1'
$publicContractPath = Join-Path $root 'tests/fixtures/distribution/public-launcher-contract.json'

Invoke-Contract 'launcher files exist' {
    Assert-True (Test-Path -LiteralPath $launcher -PathType Leaf) 'PowerShell launcher is missing'
    Assert-True (Test-Path -LiteralPath $shim -PathType Leaf) 'CMD shim is missing'
    Assert-True (Test-Path -LiteralPath $fakePath -PathType Leaf) 'fake WSL fixture is missing'
}

if (Test-Path -LiteralPath $launcher -PathType Leaf) {
    $env:GOALROUTER_LAUNCHER_TEST_MODE = '1'
    . $launcher
    Remove-Item Env:GOALROUTER_LAUNCHER_TEST_MODE
}
$productionNativeInvoker = $script:GoalRouterNativeInvoker
$productionPhysicalPathSecurityVerifier = $script:GoalRouterPhysicalPathSecurityVerifier
$script:GoalRouterPhysicalPathSecurityVerifier = { param([string]$Path) }
$script:GoalRouterPhysicalAncestorSecurityVerifier = { param([string]$Path) }
if (Test-Path -LiteralPath $fakePath -PathType Leaf) { . $fakePath }

Invoke-Contract 'production launcher native port fails closed on stderr with zero exit' {
    Assert-Throws { & $productionNativeInvoker -FilePath '/bin/sh' -Arguments @('-c', 'printf warning >&2; exit 0') -CaptureOutput $true } 'stderr|successful exit' 'launcher stderr with zero exit'
}

Invoke-Contract 'parser preserves launcher values and forwarded order' {
    $parsed = ConvertFrom-GoalRouterArguments -Arguments @(
        '--project', 'C:\Work Trees\Repo! (one)', '--access', 'write',
        '--json', 'run', '--objective', 'Fix it', '--access', 'forwarded'
    )
    Assert-Equal $parsed.Project 'C:\Work Trees\Repo! (one)' 'project'
    Assert-Equal $parsed.Access 'write' 'access'
    Assert-True $parsed.Json 'JSON option'
    Assert-Equal $parsed.Forwarded @('run', '--objective', 'Fix it', '--access', 'forwarded') 'forwarded argv'
}

Invoke-Contract 'parser rejects unknown incomplete and invalid leading options' {
    foreach ($case in @(
        @{ Args = @('--unknown'); Pattern = 'unknown launcher option' },
        @{ Args = @('--project'); Pattern = '--project requires a value' },
        @{ Args = @('--access', 'admin'); Pattern = 'invalid --access' },
        @{ Args = @('--auth-mode', 'fallback'); Pattern = 'invalid --auth-mode' }
    )) {
        $arguments = $case.Args
        $pattern = $case.Pattern
        Assert-Throws { ConvertFrom-GoalRouterArguments -Arguments $arguments } $pattern $pattern
    }
}

Invoke-Contract 'parser and values are case-sensitive without compatibility aliases' {
    foreach ($case in @(
        @{ Args = @('--ACCESS', 'write'); Pattern = 'unknown launcher option' },
        @{ Args = @('--Json'); Pattern = 'unknown launcher option' },
        @{ Args = @('--access', 'WRITE'); Pattern = 'invalid --access' },
        @{ Args = @('--auth-mode', 'API-KEY'); Pattern = 'invalid --auth-mode' }
    )) {
        $arguments = $case.Args
        $pattern = $case.Pattern
        Assert-Throws { ConvertFrom-GoalRouterArguments -Arguments $arguments } $pattern $pattern
    }
}

Invoke-Contract 'help matches shared public launcher contract' {
    $contract = Get-Content -LiteralPath $publicContractPath -Raw | ConvertFrom-Json
    $help = Get-GoalRouterHelp
    $actualOptions = @([regex]::Matches($help, '(?m)^  (--[a-z-]+)') | ForEach-Object { $_.Groups[1].Value })
    $actualCommands = @([regex]::Matches($help, '(?m)^  (doctor|update|version|uninstall)\b') | ForEach-Object { $_.Groups[1].Value })
    Assert-Equal $actualOptions @($contract.options) 'public options'
    Assert-Equal $actualCommands @($contract.maintenance_commands) 'maintenance commands'
    Assert-True ($help -notmatch 'OPENAI_API_KEY=') 'help exposes no secret-value syntax'
}

Invoke-Contract 'path resolver rejects ambiguous paths before native invocation' {
    $script:GoalRouterNativeInvoker = { throw 'native seam must not run' }
    foreach ($candidate in @('relative\repo', '\\server\share\repo', 'Registry::HKEY_CURRENT_USER\Software')) {
        $value = $candidate
        Assert-Throws {
            Resolve-GoalRouterPath -Path $value -Kind Directory -Label project -Distribution Ubuntu
        } 'absolute local drive|UNC|FileSystem provider' "reject $candidate"
    }
}

Invoke-Contract 'each literal path uses exact selected-distribution wslpath argv' {
    $state = New-FakeWslState
    Add-FakePath -State $state -Path 'C:\Work Trees\Repo! (one)' -ProviderPath 'C:\Work Trees\Repo! (one)'
    $script:GoalRouterPathResolver = New-FakePathResolver -State $state
    $script:GoalRouterNativeInvoker = New-FakeWslInvoker -State $state
    $translated = Resolve-GoalRouterPath -Path 'C:\Work Trees\Repo! (one)' -Kind Directory -Label project -Distribution 'Ubuntu-24.04'
    Assert-Equal $translated '/mnt/c/Work Trees/Repo! (one)' 'translated path'
    Assert-Equal $state.Calls.Count 1 'one translation invocation'
    Assert-Equal $state.Calls[0].Arguments @('-d', 'Ubuntu-24.04', '--exec', 'wslpath', '-a', '-u', '--', 'C:\Work Trees\Repo! (one)') 'provider-native wslpath argv'
}

Invoke-Contract 'launcher rejects relative POSIX wslpath output' {
    $state = New-FakeWslState
    Add-FakePath -State $state -Path 'C:\Work Trees\Repo! (one)' -ProviderPath 'C:\Work Trees\Repo! (one)'
    $script:GoalRouterPathResolver = New-FakePathResolver -State $state
    $script:GoalRouterNativeInvoker = { [pscustomobject]@{ ExitCode = 0; Output = @('relative/path') } }
    Assert-Throws {
        Resolve-GoalRouterPath -Path 'C:\Work Trees\Repo! (one)' -Kind Directory -Label project -Distribution 'Ubuntu-24.04'
    } 'wslpath returned an invalid project path' 'relative POSIX wslpath output'
}

Invoke-Contract 'launcher rejects Windows-looking wslpath output' {
    $state = New-FakeWslState
    Add-FakePath -State $state -Path 'C:\Work Trees\Repo! (one)' -ProviderPath 'C:\Work Trees\Repo! (one)'
    $script:GoalRouterPathResolver = New-FakePathResolver -State $state
    $script:GoalRouterNativeInvoker = { [pscustomobject]@{ ExitCode = 0; Output = @('C:\unexpected') } }
    Assert-Throws {
        Resolve-GoalRouterPath -Path 'C:\Work Trees\Repo! (one)' -Kind Directory -Label project -Distribution 'Ubuntu-24.04'
    } 'wslpath returned an invalid project path' 'Windows-looking wslpath output'
}

Invoke-Contract 'provider-native mapped UNC and wrong-kind paths fail before WSL' {
    $state = New-FakeWslState
    Add-FakePath -State $state -Path 'C:\Mapped\Repo' -ProviderPath '\\server\share\Repo'
    Add-FakePath -State $state -Path 'C:\Config' -ProviderPath 'C:\Config' -IsContainer $true -IsLeaf $false
    $script:GoalRouterPathResolver = New-FakePathResolver -State $state
    $script:GoalRouterNativeInvoker = New-FakeWslInvoker -State $state
    Assert-Throws {
        Resolve-GoalRouterPath -Path 'C:\Mapped\Repo' -Kind Directory -Label project -Distribution Ubuntu
    } 'provider-native.*UNC|UNC.*provider-native' 'mapped UNC rejection'
    Assert-Throws {
        Resolve-GoalRouterPath -Path 'C:\Config' -Kind File -Label config -Distribution Ubuntu
    } 'config file does not exist' 'wrong-kind rejection'
    Assert-Throws {
        Resolve-GoalRouterPath -Path 'C:\Missing' -Kind Directory -Label project -Distribution Ubuntu
    } 'project directory does not exist' 'missing path rejection'
    Assert-Equal $state.Calls.Count 0 'no native call for invalid resolved paths'
}

function New-TestContext {
    param([string]$Access = 'readonly', [string]$AuthMode = 'existing-session', [string[]]$Forwarded = @('version'))
    [pscustomobject]@{
        Distribution = 'Ubuntu-24.04'
        Access = $Access
        AuthMode = $AuthMode
        Json = $false
        Project = '/mnt/c/Work Trees/Repo'
        Config = '/mnt/c/Users/Test/AppData/Roaming/GoalRouter/task-models.yaml'
        State = '/mnt/c/Users/Test/AppData/Local/GoalRouter/state'
        CodexHome = '/mnt/c/Users/Test/.codex'
        Image = 'ghcr.io/example/goalrouter@sha256:' + ('a' * 64)
        Forwarded = $Forwarded
    }
}

function Get-Mount {
    param([string[]]$Arguments, [string]$Destination)
    $found = @()
    for ($index = 0; $index -lt $Arguments.Count - 1; $index++) {
        if ($Arguments[$index] -eq '--mount' -and $Arguments[$index + 1] -match ",dst=$([regex]::Escape($Destination))(,|$)") {
            $found += $Arguments[$index + 1]
        }
    }
    Assert-Equal $found.Count 1 "one mount for $Destination"
    return $found[0]
}

Invoke-Contract 'protocol mismatch preflights every Python application command with least authority' {
    $applicationInvocations = [ordered]@{
        config = @('config', 'template')
        version = @('version')
        models = @('models')
        route = @('route', '--task', 'documentation', '--prompt', 'Explain it')
        plan = @('plan', '--objective', 'Plan it')
        run = @('run', '--objective', 'Run it')
        status = @('status', 'run-1')
        approve = @('approve', 'run-1', 'work-1', '--approved-by', 'reviewer')
        resume = @('resume', 'run-1')
        report = @('report', 'run-1')
    }
    foreach ($entry in $applicationInvocations.GetEnumerator()) {
        $probe = [pscustomobject]@{
            Calls = [System.Collections.ArrayList]::new()
            ApplicationInvoked = $false
            StateMutated = $false
        }
        $script:GoalRouterNativeInvoker = {
            param([string]$FilePath, [string[]]$Arguments, [bool]$CaptureOutput)
            [void]$probe.Calls.Add([pscustomobject]@{ FilePath = $FilePath; Arguments = @($Arguments); CaptureOutput = $CaptureOutput })
            $joined = $Arguments -join "`n"
            if ($joined -notmatch 'dst=/state' -and $Arguments[-2] -ceq '--json' -and $Arguments[-1] -ceq 'version') {
                return [pscustomobject]@{ ExitCode = 0; Output = '{"version":"2.0.0","protocol_version":2}' }
            }
            $probe.ApplicationInvoked = $true
            if ($joined -match 'dst=/state') { $probe.StateMutated = $true }
            return [pscustomobject]@{ ExitCode = 0; Output = 'SDK_INITIALIZATION_MUST_NOT_RUN' }
        }.GetNewClosure()
        $context = New-TestContext -Forwarded $entry.Value
        $context.Json = $true
        $failureMessage = $null
        try { [void](Invoke-GoalRouterContext -Context $context) }
        catch {
            $failureMessage = [string]$_.Exception.Message
            [void]$Error.RemoveAt(0)
        }
        Assert-Equal $failureMessage 'Launcher protocol 1 cannot run image protocol 2.' "protocol failure $($entry.Key)"
        $failureJson = ConvertTo-GoalRouterFailureRecord -Message $failureMessage | ConvertTo-Json -Compress
        Assert-Equal $failureJson '{"status":"error","code":"launcher_protocol_mismatch","message":"Launcher protocol 1 cannot run image protocol 2."}' "exact JSON $($entry.Key)"
        Assert-Equal $probe.Calls.Count 1 "one version preflight $($entry.Key)"
        Assert-True $probe.Calls[0].CaptureOutput "captured version preflight $($entry.Key)"
        $preflight = $probe.Calls[0].Arguments
        Assert-Equal $preflight[-2..-1] @('--json', 'version') "version probe $($entry.Key)"
        $preflightText = $preflight -join "`n"
        Assert-True ($preflightText -notmatch '/project|/state|docker\.sock') "least-authority probe $($entry.Key)"
        Assert-True (-not $probe.ApplicationInvoked) "no application marker $($entry.Key)"
        Assert-True (-not $probe.StateMutated) "no state mutation $($entry.Key)"
    }
}

Invoke-Contract 'readonly Docker argv has minimal authority and no POSIX user override' {
    $argv = New-GoalRouterDockerArguments -Context (New-TestContext)
    Assert-Equal $argv[0..4] @('run', '--rm', '--read-only', '--tmpfs', '/tmp:rw,exec,nosuid,size=1g,mode=1777') 'Docker prefix'
    Assert-Equal (Get-Mount $argv '/project') 'type=bind,src=/mnt/c/Work Trees/Repo,dst=/project,readonly' 'project mount'
    Assert-Equal (Get-Mount $argv '/state') 'type=bind,src=/mnt/c/Users/Test/AppData/Local/GoalRouter/state,dst=/state' 'state mount'
    Assert-True ((Get-Mount $argv '/config/task-models.yaml').EndsWith(',readonly')) 'config is read-only'
    Assert-True ((Get-Mount $argv '/codex-auth').EndsWith(',readonly')) 'Codex home is read-only'
    Assert-True ('--user' -notin $argv) 'Windows does not set POSIX user'
    Assert-True ('/var/run/docker.sock:/var/run/docker.sock:rw' -notin $argv) 'socket absent'
    $expectedImage = 'ghcr.io/example/goalrouter@sha256:' + ('a' * 64)
    Assert-Equal $argv[-2..-1] @($expectedImage, 'version') 'image and command'
}

Invoke-Contract 'write and docker authority differ only at project and socket' {
    $write = New-GoalRouterDockerArguments -Context (New-TestContext -Access write)
    $docker = New-GoalRouterDockerArguments -Context (New-TestContext -Access docker)
    Assert-Equal (Get-Mount $write '/project') 'type=bind,src=/mnt/c/Work Trees/Repo,dst=/project' 'write project'
    Assert-True ('/var/run/docker.sock:/var/run/docker.sock:rw' -notin $write) 'write socket absent'
    Assert-Equal (Get-Mount $docker '/project') 'type=bind,src=/mnt/c/Work Trees/Repo,dst=/project' 'docker project'
    Assert-True ('/var/run/docker.sock:/var/run/docker.sock:rw' -in $docker) 'docker socket present'
}

Invoke-Contract 'JSON and Python arguments preserve exact order' {
    $context = New-TestContext -Forwarded @('run', '--objective', 'Fix it', '--json', 'literal')
    $context.Json = $true
    $argv = New-GoalRouterDockerArguments -Context $context
    Assert-Equal $argv[-7..-1] @($context.Image, '--json', 'run', '--objective', 'Fix it', '--json', 'literal') 'forwarded order'
}

Invoke-Contract 'API-key Docker argv forwards only the name and omits Codex auth' {
    $env:OPENAI_API_KEY = 'test-api-key-that-must-never-be-recorded'
    try {
        $argv = New-GoalRouterDockerArguments -Context (New-TestContext -AuthMode api-key)
        $joined = $argv -join "`n"
        Assert-True ($joined -notmatch [regex]::Escape($env:OPENAI_API_KEY)) 'secret absent from argv'
        Assert-True ('OPENAI_API_KEY' -in $argv) 'variable name forwarded'
        Assert-True ($joined -notmatch '/codex-auth') 'Codex mount omitted'
    } finally { Remove-Item Env:OPENAI_API_KEY }
}

Invoke-Contract 'image references mirror installed and override grammar outcomes' {
    foreach ($valid in @('goalrouter', 'team/my--image', 'team/my__image', 'localhost:5000/team/image:tag', 'REGISTRY.Example.COM:5000/team/image:Release_1', '[2001:db8::1]:5000/team/image:tag', ('repo/image:tag@sha256:' + ('a' * 64)), ('sha256:' + ('a' * 64)))) {
        Assert-True (Test-GoalRouterImageOverride -Image $valid) "valid image $valid"
    }
    foreach ($invalid in @('', '--privileged', 'repo//image', 'Repo/image', 'LOCALHOST/team/image:tag', 'repo/image:', 'repo/image:bad tag', ('repo/image@sha256:' + ('A' * 64)), 'registry.example.com:port/repo/image', 'bad_host.example/repo/image', 'team/my___image')) {
        Assert-True (-not (Test-GoalRouterImageOverride -Image $invalid)) "invalid image $invalid"
    }
}

Invoke-Contract 'trusted installed image remains immutable for maintenance and runtime argv' {
    $trusted = [pscustomobject]@{ image_reference = 'registry.example/goalrouter'; image_digest = ('sha256:' + ('a' * 64)) }
    $implicit = [pscustomobject]@{ ImageIsExplicit = $false; Image = $null }
    $exact = [pscustomobject]@{ ImageIsExplicit = $true; Image = ('registry.example/goalrouter@sha256:' + ('a' * 64)) }
    $foreign = [pscustomobject]@{ ImageIsExplicit = $true; Image = 'registry.example/goalrouter:other' }
    Assert-Equal (Select-GoalRouterImage -Parsed $implicit -TrustedInstall $trusted -StateWindows 'unused' -RequireTrustedImage $true) $exact.Image 'implicit installed digest'
    Assert-Equal (Select-GoalRouterImage -Parsed $exact -TrustedInstall $trusted -StateWindows 'unused' -RequireTrustedImage $true) $exact.Image 'matching explicit installed digest'
    Assert-Throws { Select-GoalRouterImage -Parsed $foreign -TrustedInstall $trusted -StateWindows 'unused' -RequireTrustedImage $true } 'explicit image.*trusted' 'foreign maintenance image'
    Assert-Equal (Select-GoalRouterImage -Parsed $foreign -TrustedInstall $trusted -StateWindows 'unused') $foreign.Image 'ordinary installed run preserves explicit image override'
    $context = New-TestContext
    $context.Image = Select-GoalRouterImage -Parsed $implicit -TrustedInstall $trusted -StateWindows 'unused' -RequireTrustedImage $true
    Assert-Equal (New-GoalRouterDockerArguments -Context $context)[-2] $exact.Image 'Docker argv uses trusted immutable digest'
}

Invoke-Contract 'launcher preflights then invokes Docker using the installed distribution' {
    $state = New-FakeWslState
    $state.DockerExitCode = 37
    $script:GoalRouterNativeInvoker = New-FakeWslInvoker -State $state
    $context = New-TestContext
    $exitCode = Invoke-GoalRouterContext -Context $context
    Assert-Equal $exitCode 37 'native exit code'
    Assert-Equal $state.Calls.Count 2 'one protocol preflight and one application invocation'
    Assert-Equal $state.Calls[0].Arguments[-2..-1] @('--json', 'version') 'protocol preflight'
    Assert-Equal $state.Calls[1].Arguments[0..3] @('-d', 'Ubuntu-24.04', '--', 'docker') 'Docker WSL prefix'
    Assert-Equal $state.Calls[1].Arguments[4..($state.Calls[1].Arguments.Count - 1)] (New-GoalRouterDockerArguments -Context $context) 'Docker argv'
}

Invoke-Contract 'launcher reads its WSL distribution from installed metadata' {
    $testRoot = Join-Path ([System.IO.Path]::GetTempPath()) 'goalrouter-powershell-installed-context'
    if (Test-Path -LiteralPath $testRoot) { Remove-Item -LiteralPath $testRoot -Recurse -Force }
    [void](New-Item -ItemType Directory -Path (Join-Path $testRoot 'GoalRouter') -Force)
    [System.IO.File]::WriteAllText((Join-Path $testRoot 'GoalRouter/install.json'), '{"wsl_distribution":"Debian-Selected"}')
    $priorLocalAppData = $env:LOCALAPPDATA
    $priorAppData = $env:APPDATA
    $priorUserProfile = $env:USERPROFILE
    try {
        $env:LOCALAPPDATA = $testRoot
        $env:APPDATA = 'C:\Profile\AppData\Roaming'
        $env:USERPROFILE = 'C:\Profile'
        $state = New-FakeWslState
        Add-FakePath -State $state -Path 'C:\Project' -ProviderPath 'C:\Project'
        Add-FakePath -State $state -Path 'C:\Config\task-models.yaml' -ProviderPath 'C:\Config\task-models.yaml' -IsContainer $false -IsLeaf $true
        Add-FakePath -State $state -Path 'C:\State' -ProviderPath 'C:\State'
        Add-FakePath -State $state -Path 'C:\Codex' -ProviderPath 'C:\Codex'
        $script:GoalRouterPathResolver = New-FakePathResolver -State $state
        $script:GoalRouterNativeInvoker = New-FakeWslInvoker -State $state
        Assert-Equal (Invoke-GoalRouterLauncher -Arguments @('--project', 'C:\Project', '--config', 'C:\Config\task-models.yaml', '--state-dir', 'C:\State', '--codex-home', 'C:\Codex', '--image', 'goalrouter', 'version')) 0 'launcher status'
        Assert-Equal $state.Calls.Count 6 'four translations plus preflight and application Docker invocations'
        foreach ($call in $state.Calls) {
            Assert-Equal $call.Arguments[0..1] @('-d', 'Debian-Selected') 'metadata distribution'
        }
        Assert-Equal $state.Calls[-1].Arguments[2..3] @('--', 'docker') 'final Docker invocation'
    } finally {
        $env:LOCALAPPDATA = $priorLocalAppData
        $env:APPDATA = $priorAppData
        $env:USERPROFILE = $priorUserProfile
        if (Test-Path -LiteralPath $testRoot) { Remove-Item -LiteralPath $testRoot -Recurse -Force }
    }
}

Invoke-Contract 'WSLENV is restored exactly for every prior state and Docker outcome' {
    $secret = 'test-api-key-that-must-never-be-recorded'
    $cases = @(
        @{ Present = $false; Value = $null; Exit = 0; Throws = $false },
        @{ Present = $true; Value = ''; Exit = 9; Throws = $false },
        @{ Present = $true; Value = 'OTHER/p:VALUE/l'; Exit = 0; Throws = $false },
        @{ Present = $true; Value = 'OPENAI_API_KEY/u:OTHER/p'; Exit = 0; Throws = $true }
    )
    foreach ($case in $cases) {
        $state = New-FakeWslState
        $script:GoalRouterEnvironmentAccessor = New-FakeEnvironmentAccessor -State $state
        if ($case.Present) { Set-GoalRouterProcessEnvironmentValue -Name 'WSLENV' -Value $case.Value }
        else { Remove-GoalRouterProcessEnvironmentValue -Name 'WSLENV' }
        $env:OPENAI_API_KEY = $secret
        $state.DockerExitCode = $case.Exit
        $state.ThrowOnDocker = $case.Throws
        $script:GoalRouterNativeInvoker = New-FakeWslInvoker -State $state
        $context = New-TestContext -AuthMode api-key
        if ($case.Throws) {
            Assert-Throws { Invoke-GoalRouterContext -Context $context } 'simulated native failure' 'thrown native error'
        } else {
            Assert-Equal (Invoke-GoalRouterContext -Context $context) $case.Exit 'native status'
        }
        $expectedDuringDocker = 'OPENAI_API_KEY/u'
        if ($case.Value) {
            if ($case.Value -match '(^|:)OPENAI_API_KEY/u(:|$)') { $expectedDuringDocker = $case.Value }
            else { $expectedDuringDocker = $case.Value + ':OPENAI_API_KEY/u' }
        }
        Assert-Equal $state.WslEnvAtDocker $expectedDuringDocker 'qualified WSLENV entry available to Docker fake'
        $restored = Get-GoalRouterProcessEnvironmentValue -Name 'WSLENV'
        Assert-Equal $restored.Present $case.Present 'WSLENV presence restored'
        if ($case.Present) { Assert-Equal $restored.Value $case.Value 'WSLENV exact value' }
        $capturedArguments = ($state.Calls | ForEach-Object { $_.Arguments -join "`n" }) -join "`n"
        Assert-True ($capturedArguments -notmatch [regex]::Escape($secret)) 'secret absent from captured calls'
    }
    if (Test-Path Env:OPENAI_API_KEY) { Remove-Item Env:OPENAI_API_KEY }
}

Invoke-Contract 'API-key installed doctor bridges and restores WSLENV without exposing the key' {
    foreach ($prior in @(
        @{ Present = $false; Value = $null },
        @{ Present = $true; Value = '' },
        @{ Present = $true; Value = 'OTHER/p' }
    )) {
        $state = New-FakeWslState
        $script:GoalRouterEnvironmentAccessor = New-FakeEnvironmentAccessor -State $state
        if ($prior.Present) { Set-GoalRouterProcessEnvironmentValue -Name WSLENV -Value $prior.Value } else { Remove-GoalRouterProcessEnvironmentValue -Name WSLENV }
        $env:OPENAI_API_KEY = 'doctor-test-secret'
        $calls = [System.Collections.ArrayList]::new()
        $script:GoalRouterNativeInvoker = { param([string]$FilePath, [string[]]$Arguments, [bool]$CaptureOutput); [void]$calls.Add(($Arguments -join ' ')); [pscustomobject]@{ ExitCode = 0; Output = @() } }.GetNewClosure()
        $context = New-TestContext -AuthMode api-key
        Assert-Equal (Invoke-GoalRouterInstalledDoctor -Context $context -SkipAccount $true) 0 'API-key doctor status'
        $after = Get-GoalRouterProcessEnvironmentValue -Name WSLENV
        Assert-Equal $after.Present $prior.Present 'WSLENV presence restored after doctor'
        if ($prior.Present) { Assert-Equal $after.Value $prior.Value 'WSLENV bytes restored after doctor' }
        Assert-True ((@($calls) -join "`n") -notmatch 'doctor-test-secret') 'doctor key absent from argv'
    }
    Remove-Item Env:OPENAI_API_KEY
}

Invoke-Contract 'doctor state write probe cleans residue after a write failure' {
    $removed = [System.Collections.ArrayList]::new()
    $write = { param([string]$Path); throw 'injected probe write failure' }
    $remove = { param([string]$Path); [void]$removed.Add($Path) }.GetNewClosure()
    Assert-Throws { Invoke-GoalRouterStateWriteProbe -Path 'D:\State\.goalrouter-doctor-test.tmp' -WriteProbe $write -RemoveProbe $remove } 'probe write failure' 'doctor probe failure'
    Assert-Equal $removed @('D:\State\.goalrouter-doctor-test.tmp') 'probe cleanup attempted in finally'
}

Invoke-Contract 'process environment abstraction distinguishes empty from absent' {
    $state = New-FakeWslState
    $script:GoalRouterEnvironmentAccessor = New-FakeEnvironmentAccessor -State $state
    Set-GoalRouterProcessEnvironmentValue -Name 'WSLENV' -Value ''
    $empty = Get-GoalRouterProcessEnvironmentValue -Name 'WSLENV'
    Assert-True $empty.Present 'empty variable is present'
    Assert-Equal $empty.Value '' 'empty value preserved'
    Remove-GoalRouterProcessEnvironmentValue -Name 'WSLENV'
    $absent = Get-GoalRouterProcessEnvironmentValue -Name 'WSLENV'
    Assert-True (-not $absent.Present) 'removed variable is absent'
}

Invoke-Contract 'Win32 zero-result classification is exact and pure' {
    Initialize-GoalRouterNativeEnvironment
    $empty = [GoalRouter.NativeEnvironment]::ClassifyZeroResult(0)
    Assert-True $empty.Present 'Win32 success zero means present-empty'
    Assert-Equal $empty.Value '' 'Win32 empty value'
    $absent = [GoalRouter.NativeEnvironment]::ClassifyZeroResult(203)
    Assert-True (-not $absent.Present) 'ERROR_ENVVAR_NOT_FOUND means absent'
    Assert-Throws { [GoalRouter.NativeEnvironment]::ClassifyZeroResult(5) } 'Exception calling "ClassifyZeroResult"|Input/output error|Access is denied' 'unexpected Win32 read error propagates'
}

Invoke-Contract 'unexpected environment read failure permits only the least-authority preflight' {
    $env:OPENAI_API_KEY = 'test-api-key-that-must-never-be-recorded'
    try {
        $state = New-FakeWslState
        $state.Environment['WSLENV'] = [pscustomobject]@{ Present = $true; Value = 'ORIGINAL/p' }
        $state.ThrowOnEnvironmentGet = $true
        $script:GoalRouterEnvironmentAccessor = New-FakeEnvironmentAccessor -State $state
        $script:GoalRouterNativeInvoker = New-FakeWslInvoker -State $state
        Assert-Throws { Invoke-GoalRouterContext -Context (New-TestContext -AuthMode api-key) } 'simulated process-environment read failure' 'environment read failure'
        Assert-Equal $state.Calls.Count 1 'only protocol preflight before environment read failure'
        Assert-Equal $state.Calls[0].Arguments[-2..-1] @('--json', 'version') 'version-only preflight'
        Assert-True (($state.Calls[0].Arguments -join "`n") -notmatch '/project|/state|docker\.sock') 'preflight has no project state or socket authority'
        Assert-True $state.Environment['WSLENV'].Present 'WSLENV presence unchanged'
        Assert-Equal $state.Environment['WSLENV'].Value 'ORIGINAL/p' 'WSLENV value unchanged'
    } finally { Remove-Item Env:OPENAI_API_KEY }
}

Invoke-Contract 'API-key mode rejects an absent or empty key before native invocation' {
    if (Test-Path Env:OPENAI_API_KEY) { Remove-Item Env:OPENAI_API_KEY }
    $state = New-FakeWslState
    $script:GoalRouterNativeInvoker = New-FakeWslInvoker -State $state
    Assert-Throws { Invoke-GoalRouterLauncher -Arguments @('--auth-mode', 'api-key', 'version') } 'OPENAI_API_KEY is required' 'missing key launcher preflight'
    $env:OPENAI_API_KEY = ''
    Assert-Throws { Invoke-GoalRouterContext -Context (New-TestContext -AuthMode api-key) } 'OPENAI_API_KEY is required' 'empty key'
    Assert-Equal $state.Calls.Count 0 'no native invocation'
    Remove-Item Env:OPENAI_API_KEY
}

Invoke-Contract 'stable native failure categories include lowercase Codex authentication' {
    $cases = [ordered]@{
        prerequisite = 'Cannot connect to the Docker daemon'
        configuration = 'configuration schema rejected'
        authentication = 'codex session is missing'
        registry = 'registry manifest unknown for image'
        mount = 'invalid mount specification'
        permission = 'permission denied by runtime'
        application = 'agent execution failed'
    }
    foreach ($entry in $cases.GetEnumerator()) {
        Assert-Equal (Get-GoalRouterFailureCode -Message $entry.Value) $entry.Key "category $($entry.Key)"
    }
}

Invoke-Contract 'JSON runtime failures are captured categorized and redacted' {
    $cases = [ordered]@{
        prerequisite = 'Cannot connect to the Docker daemon task8-secret'
        configuration = 'configuration schema rejected task8-secret'
        authentication = 'codex session expired task8-secret'
        registry = 'registry manifest unknown task8-secret'
        mount = 'invalid mount specification task8-secret'
        permission = 'permission denied by runtime task8-secret'
        application = 'agent execution failed task8-secret'
    }
    foreach ($entry in $cases.GetEnumerator()) {
        $state = New-FakeWslState
        $script:GoalRouterEnvironmentAccessor = New-FakeEnvironmentAccessor -State $state
        $state.DockerExitCode = 42
        $state.DockerOutput = $entry.Value
        $script:GoalRouterNativeInvoker = New-FakeWslInvoker -State $state
        $context = New-TestContext
        $context.Json = $true
        $failureMessage = $null
        try { [void](Invoke-GoalRouterContext -Context $context) }
        catch {
            $failureMessage = [string]$_.Exception.Message
            [void]$Error.RemoveAt(0)
        }
        Assert-Equal $failureMessage $entry.Value "captured runtime failure $($entry.Key)"
        $record = ConvertTo-GoalRouterFailureRecord -Message $failureMessage
        Assert-Equal $record.code $entry.Key "serialized category $($entry.Key)"
        Assert-Equal $record.message "GoalRouter launcher failed in the $($entry.Key) category." "redacted message $($entry.Key)"
        Assert-True ($record.message -notmatch 'task8-secret') "secret redacted $($entry.Key)"
        Assert-True $state.Calls[0].CaptureOutput "JSON native capture $($entry.Key)"
    }
}

Invoke-Contract 'JSON request detection skips launcher option values structurally' {
    Assert-True (Test-GoalRouterJsonRequested -Arguments @('--project', 'C:\Project', '--json', 'models')) 'JSON after project value'
    Assert-True (Test-GoalRouterJsonRequested -Arguments @('--config', 'C:\Config\task-models.yaml', '--json', 'models')) 'JSON after config value'
    Assert-True (-not (Test-GoalRouterJsonRequested -Arguments @('--project', 'C:\json', 'models'))) 'JSON-like value is not an option'
}

function Invoke-LauncherChild {
    param([string[]]$Arguments, [bool]$TestMode)
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = 'pwsh'
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    foreach ($argument in $Arguments) { [void]$startInfo.ArgumentList.Add($argument) }
    if ($TestMode) { $startInfo.Environment['GOALROUTER_LAUNCHER_TEST_MODE'] = '1' }
    else { [void]$startInfo.Environment.Remove('GOALROUTER_LAUNCHER_TEST_MODE') }
    [void]$startInfo.Environment.Remove('APPDATA')
    [void]$startInfo.Environment.Remove('LOCALAPPDATA')
    [void]$startInfo.Environment.Remove('USERPROFILE')
    $process = [System.Diagnostics.Process]::Start($startInfo)
    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    return [pscustomobject]@{ ExitCode = $process.ExitCode; Stdout = $stdout; Stderr = $stderr }
}

Invoke-Contract 'test mode suppresses only explicit dot-sourced loading' {
    $direct = Invoke-LauncherChild -Arguments @('-NoLogo', '-NoProfile', '-File', $launcher, '--unknown') -TestMode $true
    Assert-True ($direct.ExitCode -ne 0) 'direct execution cannot be bypassed'
    Assert-True ($direct.Stderr -match 'unknown launcher option') 'direct execution ran launcher'
    $callCommand = "& '$($launcher.Replace("'", "''"))' --unknown"
    $callOperator = Invoke-LauncherChild -Arguments @('-NoLogo', '-NoProfile', '-Command', $callCommand) -TestMode $true
    Assert-True ($callOperator.ExitCode -ne 0) 'call-operator execution cannot inherit a bypass'
    Assert-True ($callOperator.Stderr -match 'unknown launcher option') 'call-operator execution ran launcher'
    $command = "& { . '$($launcher.Replace("'", "''"))'; Write-Output 'BYPASS-MARKER' }"
    $dotSourceWithoutMode = Invoke-LauncherChild -Arguments @('-NoLogo', '-NoProfile', '-Command', $command) -TestMode $false
    Assert-True ($dotSourceWithoutMode.ExitCode -ne 0) "dot-source without explicit mode invokes normally; exit=$($dotSourceWithoutMode.ExitCode); stdout=$($dotSourceWithoutMode.Stdout); stderr=$($dotSourceWithoutMode.Stderr)"
    Assert-True ($dotSourceWithoutMode.Stdout -notmatch 'BYPASS-MARKER') 'normal invocation exits before marker'
    Assert-True ($null -ne (Get-Command ConvertFrom-GoalRouterArguments -ErrorAction SilentlyContinue)) 'explicit dot-source test seam remains loaded'
}

Invoke-Contract 'direct PowerShell help is the executable public contract' {
    $helpResult = Invoke-LauncherChild -Arguments @('-NoLogo', '-NoProfile', '-File', $launcher, '--help') -TestMode $false
    Assert-Equal $helpResult.ExitCode 0 'help exit status'
    Assert-Equal $helpResult.Stderr '' 'help stderr'
    Assert-Equal $helpResult.Stdout.Trim() (Get-GoalRouterHelp).Trim() 'direct help output'
    $invalidBeforeHelp = Invoke-LauncherChild -Arguments @('-NoLogo', '-NoProfile', '-File', $launcher, '--access', 'WRITE', '--help') -TestMode $false
    Assert-Equal $invalidBeforeHelp.ExitCode 0 'help precedes leading value validation'
    Assert-Equal $invalidBeforeHelp.Stderr '' 'help precedence stderr'
    Assert-Equal $invalidBeforeHelp.Stdout.Trim() (Get-GoalRouterHelp).Trim() 'help precedence output'
    $forwardedHelp = ConvertFrom-GoalRouterArguments -Arguments @('run', '--help')
    Assert-True (-not $forwardedHelp.Help) 'command help is not launcher help'
    Assert-Equal $forwardedHelp.Forwarded @('run', '--help') 'command help is forwarded'
}

Invoke-Contract 'CMD shim exactly preserves all arguments and native status' {
    $expected = "@echo off`npowershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File `"%~dp0goalrouter.ps1`" %*`nexit /b %ERRORLEVEL%`n"
    $actual = [System.IO.File]::ReadAllText($shim)
    Assert-Equal $actual $expected 'exact CMD shim bytes'
}

Invoke-Contract 'production syntax has a Windows PowerShell 5.1 static gate' {
    $tokens = $null
    $parseErrors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($launcher, [ref]$tokens, [ref]$parseErrors)
    Assert-Equal @($parseErrors).Count 0 'PowerShell parser errors'
    $source = [System.IO.File]::ReadAllText($launcher)
    foreach ($forbidden in @('??', '?.', 'ForEach-Object -Parallel', '$IsWindows', 'pwsh')) {
        Assert-True (-not $source.Contains($forbidden)) "5.1-forbidden syntax $forbidden"
    }
    Assert-True ($source.Contains('[CmdletBinding()]')) 'advanced script binding'
    Assert-True ($source.Contains('Set-StrictMode -Version Latest')) 'strict mode'
    Assert-True ($source.Contains("`$ErrorActionPreference = 'Stop'")) 'stop-on-error'
    Assert-True (-not $source.Contains('Invoke-Expression')) 'no command-string execution'
    Assert-True ($source.Contains('SetEnvironmentVariableW')) 'Win32 empty-environment preservation API'
    Assert-True ($source.Contains('GetEnvironmentVariableW')) 'Win32 empty-versus-absent read API'
    Assert-True ($source.Contains('public static class NativeEnvironment')) 'PowerShell-accessible Win32 environment wrapper'
    Assert-True ($source.Contains('ERROR_ENVVAR_NOT_FOUND')) 'Win32 absent-variable distinction'
    Assert-True ($source.Contains('ClassifyZeroResult')) 'pure Win32 zero-result classification'
    Assert-True ($source.Contains('ExactSpelling = true')) 'explicit suffixed Win32 symbols'
    Assert-True ($source.Contains('[DllImport("kernel32.dll"')) 'PowerShell 5.1-compatible P/Invoke declaration'
    Assert-True ($source.Contains("`$MyInvocation.InvocationName -ceq '.'")) 'dot-source invocation-mode detector'
}

Invoke-Contract 'declared PowerShell test runtime is immutable' {
    $dockerfile = [System.IO.File]::ReadAllText((Join-Path $root 'Dockerfile'))
    Assert-True ($dockerfile.Contains('FROM mcr.microsoft.com/powershell:7.5-debian-12@sha256:')) 'PowerShell test image digest pin'
}

if ($script:Failed -ne 0) {
    [Console]::Error.WriteLine("$($script:Failed) contract(s) failed; $($script:Passed) passed")
    exit 1
}
if ($Error.Count -ne 0) {
    [Console]::Error.WriteLine("$($Error.Count) unexpected PowerShell error record(s) remained")
    exit 1
}
Write-Output "$($script:Passed) PowerShell contracts passed with zero warnings and error records."
exit 0
