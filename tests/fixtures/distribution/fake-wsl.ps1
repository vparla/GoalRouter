# SPDX-License-Identifier: MIT
# File: tests/fixtures/distribution/fake-wsl.ps1
# Purpose: Structural native-process fake for Windows launcher contracts

function New-FakeWslState {
    [pscustomobject]@{
        Calls = [System.Collections.ArrayList]::new()
        ResolveCalls = [System.Collections.ArrayList]::new()
        Paths = @{}
        Environment = @{}
        ThrowOnEnvironmentGet = $false
        DockerExitCode = 0
        DockerOutput = $null
        ProtocolVersion = 1
        ThrowOnDocker = $false
        WslEnvAtDocker = $null
    }
}

function New-FakeWslInvoker {
    param([Parameter(Mandatory = $true)]$State)
    $invoker = {
        param([string]$FilePath, [string[]]$Arguments, [bool]$CaptureOutput)
        [void]$State.Calls.Add([pscustomobject]@{ FilePath = $FilePath; Arguments = @($Arguments); CaptureOutput = $CaptureOutput })
        $commandIndex = if ($Arguments.Count -gt 3 -and $Arguments[2] -ceq '--exec') { 3 } else { [Array]::IndexOf($Arguments, '--') + 1 }
        $command = $Arguments[$commandIndex]
        if ($command -eq 'wslpath') {
            $windowsPath = $Arguments[-1].Replace('\', '/')
            return [pscustomobject]@{ ExitCode = 0; Output = ('/mnt/' + $windowsPath.Substring(0, 1).ToLowerInvariant() + $windowsPath.Substring(2)) }
        }
        if ($command -eq 'docker') {
            $joined = $Arguments -join "`n"
            if ($joined -notmatch 'dst=/state' -and $Arguments[-2] -ceq '--json' -and $Arguments[-1] -ceq 'version') {
                return [pscustomobject]@{
                    ExitCode = 0
                    Output = ('{"version":"1.0.0","protocol_version":' + [string]$State.ProtocolVersion + '}')
                }
            }
            $snapshot = Get-GoalRouterProcessEnvironmentValue -Name 'WSLENV'
            $State.WslEnvAtDocker = if ($snapshot.Present) { $snapshot.Value } else { $null }
            if ($State.ThrowOnDocker) { throw 'simulated native failure' }
            return [pscustomobject]@{ ExitCode = $State.DockerExitCode; Output = $State.DockerOutput }
        }
        throw "unexpected fake WSL command: $command"
    }.GetNewClosure()
    return $invoker
}

function New-FakePathResolver {
    param([Parameter(Mandatory = $true)]$State)
    $resolver = {
        param([string]$Path)
        [void]$State.ResolveCalls.Add($Path)
        if (-not $State.Paths.ContainsKey($Path)) { throw "fake path does not exist: $Path" }
        return $State.Paths[$Path]
    }.GetNewClosure()
    return $resolver
}

function Add-FakePath {
    param(
        [Parameter(Mandatory = $true)]$State,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ProviderPath,
        [bool]$IsContainer = $true,
        [bool]$IsLeaf = $false
    )
    $State.Paths[$Path] = [pscustomobject]@{
        ProviderName = 'FileSystem'
        ProviderPath = $ProviderPath
        IsContainer = $IsContainer
        IsLeaf = $IsLeaf
    }
}

function New-FakeEnvironmentAccessor {
    param([Parameter(Mandatory = $true)]$State)
    $accessor = {
        param([string]$Operation, [string]$Name, [AllowNull()][string]$Value)
        if ($Operation -ceq 'Get') {
            if ($State.ThrowOnEnvironmentGet) { throw 'simulated process-environment read failure' }
            if ($State.Environment.ContainsKey($Name) -and $State.Environment[$Name].Present) {
                return [pscustomobject]@{ Present = $true; Value = [string]$State.Environment[$Name].Value }
            }
            return [pscustomobject]@{ Present = $false; Value = $null }
        }
        if ($Operation -ceq 'Set') {
            $State.Environment[$Name] = [pscustomobject]@{ Present = $true; Value = [string]$Value }
            return
        }
        if ($Operation -ceq 'Remove') {
            $State.Environment[$Name] = [pscustomobject]@{ Present = $false; Value = $null }
            return
        }
        throw "unexpected environment operation: $Operation"
    }.GetNewClosure()
    return $accessor
}
