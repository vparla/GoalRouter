# SPDX-License-Identifier: MIT
# File: tests/distribution/powershell_lifecycle_contract.Tests.ps1
# Purpose: Enforce Windows install, update, uninstall, and trusted-control contracts

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
    for ($index = 0; $index -lt $addedErrorCount; $index++) { $Error.RemoveAt(0) }
}

function Invoke-Contract {
    param([string]$Name, [scriptblock]$Body)
    $priorErrorCount = $Error.Count
    try {
        & $Body
        $addedErrorCount = $Error.Count - $priorErrorCount
        if ($addedErrorCount -gt 0) {
            throw "contract left $addedErrorCount unexpected PowerShell error record(s): $($Error[0].Exception.Message)"
        }
        $script:Passed++
        Write-Output "PASS $Name"
    } catch {
        $script:Failed++
        [Console]::Error.WriteLine("FAIL ${Name}: $($_.Exception.Message) [$($_.ScriptStackTrace)]")
        $Error.Clear()
    }
}

$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
$installer = Join-Path $root 'scripts/install.ps1'
$uninstaller = Join-Path $root 'scripts/uninstall.ps1'
$launcher = Join-Path $root 'scripts/goalrouter.ps1'
$shim = Join-Path $root 'scripts/goalrouter.cmd'

Invoke-Contract 'Windows lifecycle scripts exist' {
    Assert-True (Test-Path -LiteralPath $installer -PathType Leaf) 'install.ps1 is missing'
    Assert-True (Test-Path -LiteralPath $uninstaller -PathType Leaf) 'uninstall.ps1 is missing'
}

if (Test-Path -LiteralPath $launcher -PathType Leaf) {
    $env:GOALROUTER_LAUNCHER_TEST_MODE = '1'
    . $launcher
    Remove-Item Env:GOALROUTER_LAUNCHER_TEST_MODE
    $script:GoalRouterPhysicalPathSecurityVerifier = { param([string]$Path) }
    $script:GoalRouterPhysicalAncestorSecurityVerifier = { param([string]$Path) }
}
if (Test-Path -LiteralPath $installer -PathType Leaf) { . $installer }
if (Test-Path -LiteralPath $uninstaller -PathType Leaf) { . $uninstaller }

Invoke-Contract 'default and custom layouts are exact' {
    $default = Get-GoalRouterWindowsLayout -LocalAppData 'C:\Users\Me\AppData\Local' -AppData 'C:\Users\Me\AppData\Roaming' -UserProfile 'C:\Users\Me'
    Assert-Equal $default.InstallRoot 'C:\Users\Me\AppData\Local\GoalRouter' 'default install root'
    Assert-Equal $default.BinDir 'C:\Users\Me\AppData\Local\GoalRouter\bin' 'default bin'
    Assert-Equal $default.ConfigFile 'C:\Users\Me\AppData\Roaming\GoalRouter\task-models.yaml' 'default config'
    Assert-Equal $default.StateDir 'C:\Users\Me\AppData\Local\GoalRouter\state' 'default state'
    Assert-Equal $default.CodexHome 'C:\Users\Me\.codex' 'default Codex home'
    Assert-Equal $default.ManifestPath 'C:\Users\Me\AppData\Local\GoalRouter\install.json' 'trusted control path'

    $custom = Get-GoalRouterWindowsLayout -LocalAppData 'C:\L' -AppData 'C:\R' -UserProfile 'C:\U' -InstallRoot 'D:\Apps\GoalRouter' -BinDir 'D:\Tools\GoalRouter' -ConfigFile 'D:\Config\router.yaml' -StateDir 'D:\Data\Router' -CodexHome 'D:\Codex'
    Assert-Equal $custom.BinDir 'D:\Tools\GoalRouter' 'custom bin'
    Assert-Equal $custom.ConfigFile 'D:\Config\router.yaml' 'custom config'
    Assert-Equal $custom.StateDir 'D:\Data\Router' 'custom state'
    Assert-Equal $custom.CodexHome 'D:\Codex' 'custom Codex home'
    Assert-Equal $custom.ManifestPath 'D:\Apps\GoalRouter\install.json' 'custom trusted manifest'
    foreach ($collision in @('D:\Config\.goalrouter-owned-v1', 'D:\Config\.GOALROUTER-OWNED-V1', 'D:\Config\.goalrouter-owned-v1.')) {
        Assert-Throws { Get-GoalRouterWindowsLayout -LocalAppData 'C:\L' -AppData 'C:\R' -UserProfile 'C:\U' -ConfigFile $collision } 'sentinel|config' 'config cannot collide with ownership sentinel'
    }
}

Invoke-Contract 'selected WSL distribution prefixes every native call exactly' {
    $argv = New-GoalRouterWslArguments -Distribution 'Ubuntu-24.04' -Arguments @('docker', 'version', '--format', '{{json .}}')
    Assert-Equal $argv @('-d', 'Ubuntu-24.04', '--', 'docker', 'version', '--format', '{{json .}}') 'selected WSL argv'
    Assert-Throws { New-GoalRouterWslArguments -Distribution "Ubuntu`nEvil" -Arguments @('docker', 'info') } 'invalid WSL distribution' 'control-byte distribution'
    Assert-Throws { New-GoalRouterWslArguments -Distribution 'Ubuntu' -Arguments @() } 'native arguments are required' 'empty WSL command'
}

Invoke-Contract 'release URI validation is HTTPS or explicit loopback fixture only' {
    Assert-Equal (Assert-GoalRouterReleaseUri -Uri 'https://github.com/vparla/GoalRouter/releases/download/v1.0.0' -AllowLoopbackHttp $false).Scheme 'https' 'HTTPS release'
    Assert-Equal (Assert-GoalRouterReleaseUri -Uri 'http://127.0.0.1:8123/release' -AllowLoopbackHttp $true).Host '127.0.0.1' 'loopback fixture'
    Assert-Equal (Assert-GoalRouterReleaseUri -Uri 'https://objects.example/release?X-Signature=opaque%2Bvalue' -AllowLoopbackHttp $false -AllowRedirectQuery $true).Host 'objects.example' 'signed HTTPS redirect query'
    Assert-Throws { Assert-GoalRouterReleaseUri -Uri 'http://127.0.0.1:8123/release?token=fixture' -AllowLoopbackHttp $true -AllowRedirectQuery $true } 'query' 'HTTP fixture redirect query remains forbidden'
    foreach ($case in @(
        @{ Uri = 'https://user@example.com/release'; Loopback = $false },
        @{ Uri = 'http://example.com/release'; Loopback = $true },
        @{ Uri = 'https://example.com/release?token=secret'; Loopback = $false },
        @{ Uri = 'https://example.com/release#fragment'; Loopback = $false },
        @{ Uri = 'https:\\example.com\release'; Loopback = $false },
        @{ Uri = "https://example.com/release`nnext"; Loopback = $false }
    )) {
        $candidate = $case.Uri
        $allow = $case.Loopback
        Assert-Throws { Assert-GoalRouterReleaseUri -Uri $candidate -AllowLoopbackHttp $allow } 'release URI|HTTPS|userinfo|query|fragment|control|authority|backslash|loopback' "reject $candidate"
    }
    $downloadSource = [IO.File]::ReadAllText($installer)
    $downloadBlock = $downloadSource.Substring($downloadSource.IndexOf('$download = {'), $downloadSource.IndexOf('$getUserPath = {') - $downloadSource.IndexOf('$download = {'))
    Assert-True $downloadBlock.Contains('-AllowRedirectQuery $true') 'only validated redirect destinations opt into opaque HTTPS query support'
    Assert-True $downloadBlock.Contains("throw 'release download failed'") 'redirect failures remain URL-free'
    $cleanupCalls = [System.Collections.ArrayList]::new()
    $cleanupFailure = $null
    $cleanupPriorErrorCount = $Error.Count
    try { Remove-GoalRouterPartialDownload -Destination 'C:\Temp\partial' -TestPathPort { return $true } -RemovePathPort { param([string]$Path); [void]$cleanupCalls.Add($Path); throw 'https://objects.example/file?secret=query' } }
    catch { $cleanupFailure = $_.Exception.Message }
    $cleanupAddedErrorCount = $Error.Count - $cleanupPriorErrorCount
    for ($errorIndex = 0; $errorIndex -lt $cleanupAddedErrorCount; $errorIndex++) { $Error.RemoveAt(0) }
    Assert-Equal $cleanupCalls @('C:\Temp\partial') 'partial download cleanup is attempted exactly'
    Assert-True ($cleanupFailure -match 'release download failed.*cleanup failed') 'cleanup failure remains explicit'
    Assert-True ($cleanupFailure -notmatch 'objects\.example|secret=query') 'cleanup error never exposes redirect URL or query'
    Assert-True (-not $downloadBlock.Contains('SilentlyContinue')) 'download cleanup never suppresses ErrorRecords'
    Assert-Equal $cleanupPriorErrorCount 0 'no prior ErrorRecords are hidden by expected cleanup injection'
    $Error.Clear()
}

Invoke-Contract 'Windows ACL mutation masks exclude ordinary read and execute rights' {
    $expectedMutationMask = 852310
    $ordinaryReadAndExecute = 1179817
    foreach ($boundary in @(
        @{ Name = 'installer'; Mask = { Get-GoalRouterLifecycleMutationRightsMask }; Unsafe = { param([long]$Rights); Test-GoalRouterLifecycleAclRightsUnsafe -Rights $Rights } },
        @{ Name = 'launcher'; Mask = { Get-GoalRouterLauncherMutationRightsMask }; Unsafe = { param([long]$Rights); Test-GoalRouterLauncherAclRightsUnsafe -Rights $Rights } },
        @{ Name = 'bootstrap'; Mask = { Get-GoalRouterBootstrapMutationRightsMask }; Unsafe = { param([long]$Rights); Test-GoalRouterBootstrapAclRightsUnsafe -Rights $Rights } }
    )) {
        Assert-Equal (& $boundary.Mask) $expectedMutationMask "$($boundary.Name) exact mutation rights mask"
        Assert-True (-not (& $boundary.Unsafe $ordinaryReadAndExecute)) "$($boundary.Name) permits ordinary ReadAndExecute ACE"
        foreach ($dangerousRight in @(2, 4, 16, 64, 256, 65536, 262144, 524288)) {
            Assert-True (& $boundary.Unsafe $dangerousRight) "$($boundary.Name) rejects mutation right $dangerousRight"
        }
    }
}

Invoke-Contract 'checksum parser requires one exact canonical asset entry' {
    $digestA = 'a' * 64
    $digestB = 'b' * 64
    Assert-Equal (Get-GoalRouterChecksum -Text "$digestA  release-manifest.json`n$digestB *goalrouter-1.0.0-windows.zip`n" -AssetName 'release-manifest.json') $digestA 'manifest digest'
    Assert-Throws { Get-GoalRouterChecksum -Text "$digestA  release-manifest.json`n$digestA *release-manifest.json`n" -AssetName 'release-manifest.json' } 'exactly one valid checksum' 'duplicate checksum'
    Assert-Throws { Get-GoalRouterChecksum -Text "not-a-digest  release-manifest.json`n" -AssetName 'release-manifest.json' } 'exactly one valid checksum' 'invalid checksum'
    Assert-Throws { Get-GoalRouterChecksum -Text "$digestA  ../release-manifest.json`n" -AssetName 'release-manifest.json' } 'exactly one valid checksum' 'nonexact asset name'
}

function New-ZipEntry {
    param([string]$Name, [long]$ExternalAttributes = 0, [bool]$IsDirectory = $false, [long]$Length = 1024, [long]$CompressedLength = 512)
    return [pscustomobject]@{ FullName = $Name; ExternalAttributes = $ExternalAttributes; IsDirectory = $IsDirectory; Length = $Length; CompressedLength = $CompressedLength }
}

Invoke-Contract 'ZIP inspection accepts only four unique regular allowlisted files' {
    $entries = @(
        (New-ZipEntry 'goalrouter.ps1'),
        (New-ZipEntry 'goalrouter.cmd'),
        (New-ZipEntry 'install.ps1'),
        (New-ZipEntry 'uninstall.ps1')
    )
    Assert-GoalRouterZipEntries -Entries $entries
    foreach ($hostile in @(
        @($entries + (New-ZipEntry 'extra.ps1')),
        @((New-ZipEntry '../goalrouter.ps1'), $entries[1], $entries[2], $entries[3]),
        @((New-ZipEntry 'C:\goalrouter.ps1'), $entries[1], $entries[2], $entries[3]),
        @((New-ZipEntry 'goalrouter.ps1:evil'), $entries[1], $entries[2], $entries[3]),
        @($entries + (New-ZipEntry 'goalrouter.ps1')),
        @((New-ZipEntry 'goalrouter.ps1' 0 $true), $entries[1], $entries[2], $entries[3]),
        @((New-ZipEntry 'goalrouter.ps1' 0xA0000000), $entries[1], $entries[2], $entries[3]),
        @((New-ZipEntry 'goalrouter.ps1' 0x10000000), $entries[1], $entries[2], $entries[3]),
        @((New-ZipEntry 'goalrouter.ps1' 0x20000000), $entries[1], $entries[2], $entries[3]),
        @((New-ZipEntry 'goalrouter.ps1' 0x60000000), $entries[1], $entries[2], $entries[3]),
        @((New-ZipEntry 'goalrouter.ps1' 0xC0000000), $entries[1], $entries[2], $entries[3]),
        @((New-ZipEntry 'goalrouter.ps1' 0 $false 4194305 4194305), $entries[1], $entries[2], $entries[3]),
        @((New-ZipEntry 'goalrouter.ps1' 0 $false 4194304 1), $entries[1], $entries[2], $entries[3]),
        @((New-ZipEntry 'goalrouter.ps1' 0 $false 4000000 4000000), (New-ZipEntry 'goalrouter.cmd' 0 $false 4000000 4000000), (New-ZipEntry 'install.ps1' 0 $false 4000000 4000000), (New-ZipEntry 'uninstall.ps1' 0 $false 1000000 1000000))
    )) {
        $candidate = $hostile
        Assert-Throws { Assert-GoalRouterZipEntries -Entries $candidate } 'ZIP|archive|member|duplicate|unsafe|regular' 'hostile ZIP rejection'
    }
}

function New-ReleaseManifestJson {
    param(
        [string]$Version = '1.0.0',
        [int]$Protocol = 1,
        [string]$Image = 'ghcr.io/vparla/goalrouter:1.0.0',
        [string]$Digest = ('sha256:' + ('a' * 64)),
        [string[]]$Architectures = @('linux/amd64', 'linux/arm64'),
        [string]$Revision = '0123456789abcdef',
        [string]$Windows = '10.0.19045',
        [string]$PowerShell = '5.1',
        [string]$Wsl = '2.2.3',
        [string]$Docker = '20.10'
    )
    return [ordered]@{
        version = $Version
        protocol_version = $Protocol
        image = $Image
        image_digest = $Digest
        architectures = $Architectures
        source_revision = $Revision
        minimum_hosts = [ordered]@{ windows = $Windows; powershell = $PowerShell; wsl = $Wsl; docker = $Docker }
    } | ConvertTo-Json -Compress -Depth 4
}

Invoke-Contract 'release manifest binds version protocol digest revision platform and minimum hosts' {
    $manifest = Assert-GoalRouterReleaseManifest -Json (New-ReleaseManifestJson) -RequestedVersion '1.0.0' -RequestedImage 'ghcr.io/vparla/goalrouter:1.0.0' -Platform 'linux/amd64' -WindowsVersion '10.0.22631' -PowerShellVersion '5.1.22621.2506' -WslVersion '2.3.24' -DockerClientVersion '28.3.3' -DockerServerVersion '28.3.3'
    Assert-Equal $manifest.image_digest ('sha256:' + ('a' * 64)) 'bound digest'
    foreach ($case in @(
        @{ Json = New-ReleaseManifestJson -Version '1.0.1'; Pattern = 'version' },
        @{ Json = New-ReleaseManifestJson -Protocol 2; Pattern = 'protocol' },
        @{ Json = New-ReleaseManifestJson -Image 'ghcr.io/evil/payload:1.0.0'; Pattern = 'image' },
        @{ Json = New-ReleaseManifestJson -Architectures @('linux/arm64'); Pattern = 'platform' },
        @{ Json = New-ReleaseManifestJson -Architectures @('linux/amd64', 'linux/amd64'); Pattern = 'duplicated|architectures' },
        @{ Json = New-ReleaseManifestJson -Revision ''; Pattern = 'revision' },
        @{ Json = New-ReleaseManifestJson -Windows '99.0'; Pattern = 'Windows' },
        @{ Json = New-ReleaseManifestJson -PowerShell '7.0'; Pattern = 'PowerShell' },
        @{ Json = New-ReleaseManifestJson -Wsl '99.0'; Pattern = 'WSL' },
        @{ Json = New-ReleaseManifestJson -Wsl '2.2.2'; Pattern = 'wslinfo|minimum WSL' },
        @{ Json = New-ReleaseManifestJson -Docker '99.0'; Pattern = 'Docker' }
    )) {
        $json = $case.Json
        $pattern = $case.Pattern
        Assert-Throws { Assert-GoalRouterReleaseManifest -Json $json -RequestedVersion '1.0.0' -RequestedImage 'ghcr.io/vparla/goalrouter:1.0.0' -Platform 'linux/amd64' -WindowsVersion '10.0.22631' -PowerShellVersion '5.1' -WslVersion '2.3.24' -DockerClientVersion '28.3.3' -DockerServerVersion '28.3.3' } $pattern "manifest rejects $pattern"
    }
}

Invoke-Contract 'release manifest requires one canonical deterministic JSON record' {
    $canonical = New-ReleaseManifestJson
    [void](Assert-GoalRouterReleaseManifest -Json $canonical -RequestedVersion '1.0.0' -RequestedImage 'ghcr.io/vparla/goalrouter:1.0.0' -Platform 'linux/amd64' -WindowsVersion '10.0.22631' -PowerShellVersion '5.1' -WslVersion '2.3' -DockerClientVersion '28.0' -DockerServerVersion '28.0')
    [void](Assert-GoalRouterReleaseManifest -Json ($canonical + "`n") -RequestedVersion '1.0.0' -RequestedImage 'ghcr.io/vparla/goalrouter:1.0.0' -Platform 'linux/amd64' -WindowsVersion '10.0.22631' -PowerShellVersion '5.1' -WslVersion '2.3' -DockerClientVersion '28.0' -DockerServerVersion '28.0')
    [void](Assert-GoalRouterReleaseManifest -Json ($canonical + "`r`n") -RequestedVersion '1.0.0' -RequestedImage 'ghcr.io/vparla/goalrouter:1.0.0' -Platform 'linux/amd64' -WindowsVersion '10.0.22631' -PowerShellVersion '5.1' -WslVersion '2.3' -DockerClientVersion '28.0' -DockerServerVersion '28.0')
    Assert-Throws { Assert-GoalRouterReleaseManifest -Json ($canonical + "`n`n") -RequestedVersion '1.0.0' -RequestedImage 'ghcr.io/vparla/goalrouter:1.0.0' -Platform 'linux/amd64' -WindowsVersion '10.0.22631' -PowerShellVersion '5.1' -WslVersion '2.3' -DockerClientVersion '28.0' -DockerServerVersion '28.0' } 'invalid bytes' 'more than one terminal newline rejected'
    $parsed = $canonical | ConvertFrom-Json
    $reordered = [ordered]@{ protocol_version = $parsed.protocol_version; version = $parsed.version; image = $parsed.image; image_digest = $parsed.image_digest; architectures = @($parsed.architectures); source_revision = $parsed.source_revision; minimum_hosts = [ordered]@{ windows = $parsed.minimum_hosts.windows; powershell = $parsed.minimum_hosts.powershell; wsl = $parsed.minimum_hosts.wsl; docker = $parsed.minimum_hosts.docker } } | ConvertTo-Json -Compress -Depth 4
    Assert-Throws { Assert-GoalRouterReleaseManifest -Json $reordered -RequestedVersion '1.0.0' -RequestedImage 'ghcr.io/vparla/goalrouter:1.0.0' -Platform 'linux/amd64' -WindowsVersion '10.0.22631' -PowerShellVersion '5.1' -WslVersion '2.0' -DockerClientVersion '28.0' -DockerServerVersion '28.0' } 'canonical' 'reordered manifest rejected'
}

Invoke-Contract 'manifest schema rejects unknown missing and noncanonical fields' {
    $missing = '{"version":"1.0.0"}'
    Assert-Throws { Assert-GoalRouterReleaseManifest -Json $missing -RequestedVersion '1.0.0' -RequestedImage 'ghcr.io/vparla/goalrouter:1.0.0' -Platform 'linux/amd64' -WindowsVersion '10.0' -PowerShellVersion '5.1' -WslVersion '2.0' -DockerClientVersion '28.0' -DockerServerVersion '28.0' } 'schema|fields' 'missing fields'
    $payload = New-ReleaseManifestJson | ConvertFrom-Json
    $payload | Add-Member -NotePropertyName credential -NotePropertyValue 'must-not-persist'
    $unknown = $payload | ConvertTo-Json -Compress -Depth 4
    Assert-Throws { Assert-GoalRouterReleaseManifest -Json $unknown -RequestedVersion '1.0.0' -RequestedImage 'ghcr.io/vparla/goalrouter:1.0.0' -Platform 'linux/amd64' -WindowsVersion '10.0' -PowerShellVersion '5.1' -WslVersion '2.0' -DockerClientVersion '28.0' -DockerServerVersion '28.0' } 'schema|fields' 'unknown field'
}

Invoke-Contract 'User PATH addition preserves absent empty populated and duplicate states' {
    $absent = Add-GoalRouterUserPathEntry -Snapshot ([pscustomobject]@{ Present = $false; Value = $null }) -OwnedEntry 'C:\GoalRouter\bin'
    Assert-True $absent.Changed 'absent PATH changes'
    Assert-True $absent.Snapshot.Present 'absent becomes present'
    Assert-Equal $absent.Snapshot.Value 'C:\GoalRouter\bin' 'absent PATH value'
    $empty = Add-GoalRouterUserPathEntry -Snapshot ([pscustomobject]@{ Present = $true; Value = '' }) -OwnedEntry 'C:\GoalRouter\bin'
    Assert-Equal $empty.Snapshot.Value 'C:\GoalRouter\bin' 'empty PATH value'
    $populated = Add-GoalRouterUserPathEntry -Snapshot ([pscustomobject]@{ Present = $true; Value = 'C:\Tools;C:\Other' }) -OwnedEntry 'C:\GoalRouter\bin'
    Assert-Equal $populated.Snapshot.Value 'C:\Tools;C:\Other;C:\GoalRouter\bin' 'append preserves bytes and order'
    $duplicate = Add-GoalRouterUserPathEntry -Snapshot ([pscustomobject]@{ Present = $true; Value = 'C:\Tools;c:\goalrouter\BIN;C:\Other' }) -OwnedEntry 'C:\GoalRouter\bin'
    Assert-True (-not $duplicate.Changed) 'Windows-equivalent entry is not duplicated'
    Assert-Equal $duplicate.Snapshot.Value 'C:\Tools;c:\goalrouter\BIN;C:\Other' 'duplicate bytes untouched'
}

Invoke-Contract 'User PATH removal is exact installer-owned and preserves user modifications' {
    $owned = Add-GoalRouterUserPathEntry -Snapshot ([pscustomobject]@{ Present = $true; Value = 'C:\Tools'; ValueKind = 'String' }) -OwnedEntry 'C:\GoalRouter\bin'
    $removed = Remove-GoalRouterUserPathEntry -Snapshot $owned.Snapshot -Ownership $owned
    Assert-True $removed.Changed 'exact owned PATH is removed'
    Assert-Equal $removed.Snapshot.Value 'C:\Tools' 'prior populated PATH restored exactly'
    $userChanged = [pscustomobject]@{ Present = $true; Value = 'C:\GoalRouter\bin;C:\Tools'; ValueKind = 'String' }
    $preserved = Remove-GoalRouterUserPathEntry -Snapshot $userChanged -Ownership $owned
    Assert-True (-not $preserved.Changed) 'reordered PATH is user-modified'
    Assert-Equal $preserved.Snapshot.Value $userChanged.Value 'user-modified PATH untouched'
    $caseChanged = [pscustomobject]@{ Present = $true; Value = 'C:\Tools;c:\goalrouter\bin'; ValueKind = 'String' }
    Assert-True (-not (Remove-GoalRouterUserPathEntry -Snapshot $caseChanged -Ownership $owned).Changed) 'case-varied PATH is user-modified'
    $kindChanged = [pscustomobject]@{ Present = $true; Value = $owned.Snapshot.Value; ValueKind = 'ExpandString' }
    Assert-True (-not (Remove-GoalRouterUserPathEntry -Snapshot $kindChanged -Ownership $owned).Changed) 'same text with changed registry kind is user-modified'
}

Invoke-Contract 'NoPathUpdate is deterministic and never claims ownership' {
    $before = [pscustomobject]@{ Present = $true; Value = 'C:\Tools' }
    $result = Add-GoalRouterUserPathEntry -Snapshot $before -OwnedEntry 'C:\GoalRouter\bin' -NoPathUpdate
    Assert-True (-not $result.Changed) 'NoPathUpdate does not mutate'
    Assert-True (-not $result.InstallerAdded) 'NoPathUpdate records no ownership'
    Assert-Equal $result.Snapshot.Value 'C:\Tools' 'NoPathUpdate preserves bytes'
}

Invoke-Contract 'Unicode per-user paths round-trip through PATH ownership and canonical control JSON' {
    $unicodeBin = 'C:\Users\José\AppData\Local\GoalRouter\bin'
    $pathChange = Add-GoalRouterUserPathEntry -Snapshot ([pscustomobject]@{ Present = $true; Value = 'C:\Outils' }) -OwnedEntry $unicodeBin
    Assert-Equal $pathChange.Snapshot.Value ('C:\Outils;' + $unicodeBin) 'Unicode PATH addition'
    $json = ConvertTo-GoalRouterCanonicalJson ([ordered]@{ owned = [ordered]@{ bin_dir = $unicodeBin; config_file = 'C:\Users\José\Données\task-models.yaml' } })
    Assert-True $json.Contains('José') 'Unicode canonical JSON serialization'
    Assert-True (Test-GoalRouterPathText $json) 'launcher-compatible Unicode control text'
}

Invoke-Contract 'trusted lifecycle text bytes require canonical strict BOM-less UTF-8' {
    $canonicalText = '{"version":"1.0.0"}'
    $canonicalBytes = [Text.UTF8Encoding]::new($false, $true).GetBytes($canonicalText)
    Assert-Equal (ConvertFrom-GoalRouterStrictUtf8Bytes -Bytes $canonicalBytes -Label 'fixture control') $canonicalText 'canonical UTF-8 round-trip'
    $bomBytes = [byte[]]@(0xef, 0xbb, 0xbf) + $canonicalBytes
    Assert-Throws { ConvertFrom-GoalRouterStrictUtf8Bytes -Bytes $bomBytes -Label 'fixture control' } 'BOM-less' 'checksummed BOM-prefixed control'
    Assert-Throws { ConvertFrom-GoalRouterStrictUtf8Bytes -Bytes ([byte[]]@(0x7b, 0xc3, 0x28, 0x7d)) -Label 'fixture control' } 'invalid UTF-8' 'checksummed invalid UTF-8 control'
    $installerSource = [IO.File]::ReadAllText($installer)
    $launcherSource = [IO.File]::ReadAllText($launcher)
    Assert-True $installerSource.Contains('ConvertFrom-GoalRouterStrictUtf8Bytes -Bytes ([IO.File]::ReadAllBytes($Path))') 'installer production text port uses raw strict bytes'
    Assert-True $launcherSource.Contains("Read-GoalRouterTrustedUtf8Text -Path `$manifestPath") 'launcher trusted control uses raw strict bytes'
    Assert-True $launcherSource.Contains("Read-GoalRouterTrustedUtf8Text -Path `$stateManifestPath") 'launcher parity control uses raw strict bytes'
}

Invoke-Contract 'trusted install manifest has Task 6 common fields and Windows-only ownership' {
    $manifest = New-GoalRouterInstallManifest -Version '1.0.0' -ImageReference 'ghcr.io/vparla/goalrouter' -ImageDigest ('sha256:' + ('a' * 64)) -ImagePlatform 'linux/amd64' -SourceRevision '0123456789abcdef' -WslDistribution 'Ubuntu-24.04' -Layout (Get-GoalRouterWindowsLayout -LocalAppData 'C:\L' -AppData 'C:\R' -UserProfile 'C:\U') -PathOwnership ([pscustomobject]@{ InstallerAdded = $true; OwnedValue = 'C:\L\GoalRouter\bin'; Before = [pscustomobject]@{ Present = $false; Value = $null }; After = [pscustomobject]@{ Present = $true; Value = 'C:\L\GoalRouter\bin' } }) -ReleaseBase 'https://example.com/release'
    $json = ConvertTo-GoalRouterCanonicalJson -Value $manifest
    foreach ($key in @('manifest_version', 'protocol_version', 'version', 'launcher_version', 'image_reference', 'image_digest', 'image_platform', 'source_revision', 'owned', 'wsl_distribution', 'path_ownership', 'release_base')) {
        Assert-True ($json.Contains('"' + $key + '"')) "manifest contains $key"
    }
    foreach ($forbidden in @('OPENAI_API_KEY', 'Bearer ', 'credential', 'password', 'token=')) {
        Assert-True (-not $json.Contains($forbidden)) "manifest excludes $forbidden"
    }
}

Invoke-Contract 'candidate validation orders digest platform revision before first run' {
    $calls = [System.Collections.ArrayList]::new()
    $native = {
        param([string]$FilePath, [string[]]$Arguments, [bool]$CaptureOutput)
        [void]$calls.Add(@($Arguments))
        $command = $Arguments[3..($Arguments.Count - 1)]
        $joined = $command -join ' '
        if ($joined -match 'docker pull') { return [pscustomobject]@{ ExitCode = 0; Output = @() } }
        if ($joined -match '\.Architecture') { return [pscustomobject]@{ ExitCode = 0; Output = @('amd64') } }
        if ($joined -match 'RepoDigests') { return [pscustomobject]@{ ExitCode = 0; Output = @('ghcr.io/vparla/goalrouter@sha256:' + ('a' * 64)) } }
        if ($joined -match 'image\.revision') { return [pscustomobject]@{ ExitCode = 0; Output = @('0123456789abcdef') } }
        if ($joined -match 'docker run') { return [pscustomobject]@{ ExitCode = 0; Output = @('{"version":"1.0.0","protocol_version":1}') } }
        throw "unexpected native call: $joined"
    }.GetNewClosure()
    $manifest = (New-ReleaseManifestJson | ConvertFrom-Json)
    $result = Test-GoalRouterCandidateImage -Manifest $manifest -Distribution 'Ubuntu-24.04' -Platform 'linux/amd64' -NativeInvoker $native
    Assert-Equal $result.RepoDigest ('ghcr.io/vparla/goalrouter@sha256:' + ('a' * 64)) 'canonical RepoDigest'
    Assert-True ((@($calls | ForEach-Object { $_ -join ' ' }) -join "`n") -match 'docker run') 'candidate eventually executes'

    $badManifest = New-ReleaseManifestJson -Digest ('sha256:' + ('b' * 64)) | ConvertFrom-Json
    $calls.Clear()
    Assert-Throws { Test-GoalRouterCandidateImage -Manifest $badManifest -Distribution 'Ubuntu-24.04' -Platform 'linux/amd64' -NativeInvoker $native } 'digest' 'tag drift rejected'
    Assert-True ((@($calls | ForEach-Object { $_ -join ' ' }) -join "`n") -notmatch 'docker run') 'tag drift fails before candidate run'
    foreach ($call in $calls) { Assert-Equal $call[0..2] @('-d', 'Ubuntu-24.04', '--') 'every candidate WSL prefix' }
}

function New-MemoryLifecycleState {
    $state = [pscustomobject]@{
        Files = @{}
        Directories = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        Calls = [System.Collections.ArrayList]::new()
        UserPath = [pscustomobject]@{ Present = $true; Value = 'C:\Tools' }
        ThrowPhase = $null
        DoctorExitCode = 0
    }
    return $state
}

function New-MemoryLifecyclePorts {
    param([Parameter(Mandatory = $true)]$State)
    $snapshot = {
        param([string]$Path)
        [void]$State.Calls.Add("snapshot:$Path")
        if ($State.Files.ContainsKey($Path)) { return [pscustomobject]@{ Present = $true; Content = [string]$State.Files[$Path] } }
        return [pscustomobject]@{ Present = $false; Content = $null }
    }.GetNewClosure()
    $replace = {
        param([string]$Path, [string]$Content)
        [void]$State.Calls.Add("replace:$Path")
        if ($State.ThrowPhase -ceq "replace:$Path") { throw "injected replacement failure: $Path" }
        $State.Files[$Path] = $Content
    }.GetNewClosure()
    $restore = {
        param([string]$Path, $Snapshot)
        [void]$State.Calls.Add("restore:$Path")
        if ($Snapshot.Present) { $State.Files[$Path] = [string]$Snapshot.Content } else { [void]$State.Files.Remove($Path) }
    }.GetNewClosure()
    $getPath = { return Copy-GoalRouterPathSnapshot -Snapshot $State.UserPath }.GetNewClosure()
    $setPath = {
        param($Snapshot)
        [void]$State.Calls.Add('set-user-path')
        if ($State.ThrowPhase -ceq 'set-user-path') { throw 'injected User PATH failure' }
        $State.UserPath = Copy-GoalRouterPathSnapshot -Snapshot $Snapshot
    }.GetNewClosure()
    $doctor = {
        param([string]$FilePath, [string[]]$Arguments)
        [void]$State.Calls.Add([pscustomobject]@{ Kind = 'doctor'; FilePath = $FilePath; Arguments = @($Arguments) })
        if ($State.ThrowPhase -ceq 'doctor') { throw 'injected doctor failure' }
        return $State.DoctorExitCode
    }.GetNewClosure()
    $remove = {
        param([string]$Path)
        [void]$State.Calls.Add("remove:$Path")
        if ($State.ThrowPhase -ceq "remove:$Path") { throw "injected removal failure: $Path" }
        [void]$State.Files.Remove($Path)
    }.GetNewClosure()
    return [pscustomobject]@{ Snapshot = $snapshot; Replace = $replace; Restore = $restore; GetUserPath = $getPath; SetUserPath = $setPath; Doctor = $doctor; RemoveFile = $remove; RemoveTree = $remove }
}

Invoke-Contract 'atomic install commit rolls back files and exact User PATH after doctor failure' {
    $state = New-MemoryLifecycleState
    $state.Files['C:\GoalRouter\bin\goalrouter.ps1'] = 'old-launcher'
    $state.Files['C:\GoalRouter\install.json'] = 'old-control'
    $state.ThrowPhase = 'doctor'
    $ports = New-MemoryLifecyclePorts -State $state
    $pathChange = Add-GoalRouterUserPathEntry -Snapshot $state.UserPath -OwnedEntry 'C:\GoalRouter\bin'
    $plan = [pscustomobject]@{
        Replacements = [ordered]@{
            'C:\GoalRouter\bin\goalrouter.ps1' = 'new-launcher'
            'C:\GoalRouter\install.json' = 'new-control'
        }
        PathChange = $pathChange
        Doctor = [pscustomobject]@{ FilePath = 'C:\GoalRouter\bin\goalrouter.ps1'; Arguments = @('--config', 'D:\Config\task-models.yaml', '--state-dir', 'D:\State', '--codex-home', 'D:\Codex', 'doctor', '-SkipAccount') }
        SkipDoctor = $false
    }
    Assert-Throws { Invoke-GoalRouterInstallCommit -Plan $plan -Ports $ports } 'injected doctor failure' 'post-switch doctor rollback'
    Assert-Equal $state.Files['C:\GoalRouter\bin\goalrouter.ps1'] 'old-launcher' 'launcher restored'
    Assert-Equal $state.Files['C:\GoalRouter\install.json'] 'old-control' 'control restored'
    Assert-Equal $state.UserPath.Value 'C:\Tools' 'User PATH restored'
    $doctorCall = @($state.Calls | Where-Object { $_ -isnot [string] -and $_.Kind -ceq 'doctor' })[0]
    Assert-Equal $doctorCall.FilePath 'C:\GoalRouter\bin\goalrouter.ps1' 'physical doctor launcher'
    Assert-Equal $doctorCall.Arguments @('--config', 'D:\Config\task-models.yaml', '--state-dir', 'D:\State', '--codex-home', 'D:\Codex', 'doctor', '-SkipAccount') 'custom doctor propagation'
}

Invoke-Contract 'atomic update switches all files and does not duplicate PATH' {
    $state = New-MemoryLifecycleState
    $state.UserPath = [pscustomobject]@{ Present = $true; Value = 'C:\Tools;C:\GoalRouter\bin' }
    $ports = New-MemoryLifecyclePorts -State $state
    $pathChange = Add-GoalRouterUserPathEntry -Snapshot $state.UserPath -OwnedEntry 'C:\GoalRouter\bin'
    $plan = [pscustomobject]@{
        Replacements = [ordered]@{
            'C:\GoalRouter\bin\goalrouter.ps1' = 'new-launcher'
            'C:\GoalRouter\bin\install.ps1' = 'new-installer'
            'C:\GoalRouter\bin\uninstall.ps1' = 'new-uninstaller'
            'C:\GoalRouter\install.json' = 'new-control'
            'D:\State\install.json' = 'new-parity'
        }
        PathChange = $pathChange
        Doctor = [pscustomobject]@{ FilePath = 'C:\GoalRouter\bin\goalrouter.ps1'; Arguments = @('doctor') }
        SkipDoctor = $false
    }
    Invoke-GoalRouterInstallCommit -Plan $plan -Ports $ports
    Assert-Equal $state.Files['C:\GoalRouter\install.json'] 'new-control' 'control switched'
    Assert-Equal $state.Files['D:\State\install.json'] 'new-parity' 'parity switched'
    Assert-Equal $state.UserPath.Value 'C:\Tools;C:\GoalRouter\bin' 'PATH not duplicated'
}

Invoke-Contract 'trusted state parity fails closed and never grants authority' {
    $manifest = New-GoalRouterInstallManifest -Version '1.0.0' -ImageReference 'ghcr.io/vparla/goalrouter' -ImageDigest ('sha256:' + ('a' * 64)) -ImagePlatform 'linux/amd64' -SourceRevision '0123456789abcdef' -WslDistribution 'Ubuntu-Trusted' -Layout (Get-GoalRouterWindowsLayout -LocalAppData 'C:\L' -AppData 'C:\R' -UserProfile 'C:\U') -PathOwnership ([pscustomobject]@{ InstallerAdded = $false; OwnedValue = 'C:\L\GoalRouter\bin'; Before = [pscustomobject]@{ Present = $true; Value = 'C:\Tools' }; After = [pscustomobject]@{ Present = $true; Value = 'C:\Tools' } }) -ReleaseBase 'https://example.com/release'
    $trustedJson = ConvertTo-GoalRouterCanonicalJson $manifest
    Assert-GoalRouterTrustedStateParity -TrustedJson $trustedJson -StateJson $trustedJson
    $forged = $trustedJson.Replace('Ubuntu-Trusted', 'Evil-Distro').Replace(('a' * 64), ('b' * 64))
    Assert-Throws { Assert-GoalRouterTrustedStateParity -TrustedJson $trustedJson -StateJson $forged } 'parity' 'forged runtime state'
    Assert-Throws { Assert-GoalRouterTrustedStateParity -TrustedJson $trustedJson -StateJson $null } 'missing.*parity|parity.*missing' 'deleted runtime state'
    Assert-Equal $manifest.wsl_distribution 'Ubuntu-Trusted' 'trusted WSL remains authoritative'
    Assert-Equal $manifest.image_digest ('sha256:' + ('a' * 64)) 'trusted digest remains authoritative'
}

Invoke-Contract 'maintenance invocation uses only trusted physical lifecycle files and custom paths' {
    $layout = Get-GoalRouterWindowsLayout -LocalAppData 'C:\L' -AppData 'C:\R' -UserProfile 'C:\U' -InstallRoot 'D:\Install' -BinDir 'D:\Install\bin' -ConfigFile 'E:\Cfg\task-models.yaml' -StateDir 'E:\State' -CodexHome 'E:\Codex'
    $manifest = New-GoalRouterInstallManifest -Version '1.0.0' -ImageReference 'ghcr.io/vparla/goalrouter' -ImageDigest ('sha256:' + ('a' * 64)) -ImagePlatform 'linux/amd64' -SourceRevision '0123456789abcdef' -WslDistribution 'Debian-Trusted' -Layout $layout -PathOwnership ([pscustomobject]@{ InstallerAdded = $false; OwnedValue = $layout.BinDir; Before = [pscustomobject]@{ Present = $false; Value = $null }; After = [pscustomobject]@{ Present = $false; Value = $null } }) -ReleaseBase 'https://example.com/release'
    $update = New-GoalRouterMaintenanceInvocation -Command 'update' -CommandArguments @('1.0.1') -Manifest $manifest -PhysicalLauncherPath $layout.LauncherPath -AuthMode 'existing-session'
    Assert-Equal $update.FilePath $layout.InstallerPath 'trusted installer path'
    Assert-Equal $update.Arguments @('-Version', '1.0.1', '-InstallRoot', $layout.InstallRoot, '-BinDir', $layout.BinDir, '-ConfigFile', $layout.ConfigFile, '-StateDir', $layout.StateDir, '-CodexHome', $layout.CodexHome, '-WslDistribution', 'Debian-Trusted', '-ReleaseBase', 'https://example.com/release', '-Image', 'ghcr.io/vparla/goalrouter:1.0.1', '-AuthMode', 'existing-session', '-Yes', '-NoPathUpdate') 'trusted update propagation'
    $apiUpdate = New-GoalRouterMaintenanceInvocation -Command 'update' -CommandArguments @('1.0.1') -Manifest $manifest -PhysicalLauncherPath $layout.LauncherPath -AuthMode 'api-key'
    Assert-True ((@($apiUpdate.Arguments) -join ' ') -match '-AuthMode api-key') 'explicit API-key auth propagates to installer'
    Assert-True ((@($apiUpdate.Arguments) -join ' ') -notmatch 'sk-secret|OPENAI_API_KEY=') 'update argv contains no API-key value'
    $uninstall = New-GoalRouterMaintenanceInvocation -Command 'uninstall' -CommandArguments @('-Purge', '-Yes') -Manifest $manifest -PhysicalLauncherPath $layout.LauncherPath
    Assert-Equal $uninstall.FilePath $layout.UninstallerPath 'trusted uninstaller path'
    Assert-Equal $uninstall.Arguments @('-InstallRoot', $layout.InstallRoot, '-Purge', '-Yes') 'trusted uninstall argv'
    $manifest.owned.launcher = $layout.LauncherPath.Replace('\', '/')
    [void](New-GoalRouterMaintenanceInvocation -Command 'update' -CommandArguments @('1.0.0') -Manifest $manifest -PhysicalLauncherPath $layout.LauncherPath.ToLowerInvariant())
    Assert-Throws { New-GoalRouterMaintenanceInvocation -Command 'update' -CommandArguments @('1.0.1', 'extra') -Manifest $manifest -PhysicalLauncherPath $layout.LauncherPath } 'invalid update arguments' 'update extras'
    Assert-Throws { New-GoalRouterMaintenanceInvocation -Command 'uninstall' -CommandArguments @('-Purge', '-Purge') -Manifest $manifest -PhysicalLauncherPath $layout.LauncherPath } 'duplicate' 'duplicate purge'
    Assert-Throws { New-GoalRouterMaintenanceInvocation -Command 'update' -CommandArguments @() -Manifest $manifest -PhysicalLauncherPath 'C:\Forged\goalrouter.ps1' } 'physical launcher' 'noncanonical physical launcher'
}

function New-SafePathInfo {
    param([string]$Path, [bool]$Reparse = $false, [bool]$Exists = $true, [string[]]$Entries = @(), [string]$Sentinel = 'goalrouter-owned-directory-v1')
    return [pscustomobject]@{ Path = $Path; ProviderName = 'FileSystem'; ProviderPath = $Path; Exists = $Exists; IsContainer = $true; IsReparsePoint = $Reparse; ContainsReparsePoint = $false; OwnerMatchesCurrentUser = $true; AclIsSafe = $true; Entries = $Entries; Sentinel = $Sentinel }
}

Invoke-Contract 'uninstall plan preserves by default and purge prevalidates every exact target' {
    $layout = Get-GoalRouterWindowsLayout -LocalAppData 'C:\Users\Me\AppData\Local' -AppData 'C:\Users\Me\AppData\Roaming' -UserProfile 'C:\Users\Me'
    $ownership = [pscustomobject]@{ InstallerAdded = $true; OwnedValue = $layout.BinDir; Before = [pscustomobject]@{ Present = $true; Value = 'C:\Tools' }; After = [pscustomobject]@{ Present = $true; Value = 'C:\Tools;' + $layout.BinDir } }
    $manifest = New-GoalRouterInstallManifest -Version '1.0.0' -ImageReference 'ghcr.io/vparla/goalrouter' -ImageDigest ('sha256:' + ('a' * 64)) -ImagePlatform 'linux/amd64' -SourceRevision '0123456789abcdef' -WslDistribution 'Ubuntu' -Layout $layout -PathOwnership $ownership -ReleaseBase 'https://example.com/release'
    $pathInfos = @{
        Config = New-SafePathInfo -Path $layout.ConfigDir -Entries @($script:GoalRouterDirectorySentinel, 'task-models.yaml')
        State = New-SafePathInfo -Path $layout.StateDir -Entries @($script:GoalRouterDirectorySentinel, 'install.json', 'runs')
    }
    $preserve = New-GoalRouterUninstallPlan -Manifest $manifest -Purge $false -PathInfos $pathInfos -CurrentUserPath $ownership.After -RecoveryMode $null
    Assert-True ($layout.ConfigDir -cnotin $preserve.RemoveTrees) 'default preserves config'
    Assert-True ($layout.StateDir -cnotin $preserve.RemoveTrees) 'default preserves state'
    Assert-True ($layout.InstallerPath -cnotin $preserve.EarlyFiles) 'shared lifecycle helper retained through recovery phases'
    Assert-Equal $preserve.InstallerPath $layout.InstallerPath 'helper has explicit final cleanup target'
    $purge = New-GoalRouterUninstallPlan -Manifest $manifest -Purge $true -PathInfos $pathInfos -CurrentUserPath $ownership.After -RecoveryMode 'purge'
    Assert-Equal $purge.RemoveTrees @($layout.ConfigDir, $layout.StateDir) 'purge exact roots'
    Assert-Equal $purge.PathResult.Snapshot.Value 'C:\Tools' 'owned PATH removal planned'
    Assert-Throws { New-GoalRouterUninstallPlan -Manifest $manifest -Purge $false -PathInfos $pathInfos -CurrentUserPath $ownership.After -RecoveryMode 'purge' } 'recovery mode' 'same-mode recovery'

    foreach ($bad in @(
        @{ Config = New-SafePathInfo -Path $env:HOME; State = $pathInfos.State; Pattern = 'exact|recorded' },
        @{ Config = New-SafePathInfo -Path $layout.ConfigDir -Reparse $true; State = $pathInfos.State; Pattern = 'reparse' },
        @{ Config = New-SafePathInfo -Path $layout.ConfigDir -Sentinel 'foreign'; State = $pathInfos.State; Pattern = 'sentinel' }
    )) {
        $infos = @{ Config = $bad.Config; State = $bad.State }
        $pattern = $bad.Pattern
        Assert-Throws { New-GoalRouterUninstallPlan -Manifest $manifest -Purge $true -PathInfos $infos -CurrentUserPath $ownership.After -RecoveryMode $null } $pattern 'purge target prevalidation'
    }
    $preserveReparse = @{ Config = New-SafePathInfo -Path $layout.ConfigDir -Reparse $true; State = $pathInfos.State }
    Assert-Throws { New-GoalRouterUninstallPlan -Manifest $manifest -Purge $false -PathInfos $preserveReparse -CurrentUserPath $ownership.After -RecoveryMode $null } 'reparse' 'preserve refuses config junction'
    $preserveProvider = New-SafePathInfo -Path $layout.StateDir
    $preserveProvider.ProviderPath = 'D:\Redirected\State'
    Assert-Throws { New-GoalRouterUninstallPlan -Manifest $manifest -Purge $false -PathInfos @{ Config = $pathInfos.Config; State = $preserveProvider } -CurrentUserPath $ownership.After -RecoveryMode $null } 'provider|unsafe' 'preserve refuses redirected state'
}

Invoke-Contract 'uninstall validates canonical lifecycle relationships and typed file targets' {
    $layout = Get-GoalRouterWindowsLayout -LocalAppData 'C:\L' -AppData 'C:\R' -UserProfile 'C:\U' -InstallRoot 'D:\Install' -BinDir 'D:\Install\bin' -ConfigFile 'D:\Config\task-models.yaml' -StateDir 'D:\State' -CodexHome 'D:\Codex'
    $ownership = [pscustomobject]@{ InstallerAdded = $true; UpdateEnabled = $true; OwnedValue = $layout.BinDir; Before = [pscustomobject]@{ Present = $false; Value = $null }; After = [pscustomobject]@{ Present = $true; Value = $layout.BinDir } }
    $manifest = New-GoalRouterInstallManifest -Version '1.0.0' -ImageReference 'registry.example/router' -ImageDigest ('sha256:' + ('a' * 64)) -ImagePlatform 'linux/amd64' -SourceRevision 'rev' -WslDistribution Ubuntu -Layout $layout -PathOwnership $ownership -ReleaseBase 'https://example.com/release'
    $manifest = ConvertTo-GoalRouterCanonicalJson $manifest | ConvertFrom-Json
    [void](Assert-GoalRouterUninstallManifestLayout -Manifest $manifest)
    [void](Assert-GoalRouterInstalledManifestSchema -Manifest $manifest)
    foreach ($invalidControl in @(
        @{ Name = 'release userinfo'; Change = { param($value); $value.release_base = 'https://attacker@example.com/release' }; Pattern = 'release URI|runtime authority' },
        @{ Name = 'post hash without kind'; Change = { param($value); $value.path_ownership.after_sha256 = ('b' * 64); $value.path_ownership.after_value_kind = $null }; Pattern = 'PATH ownership' },
        @{ Name = 'post kind without hash'; Change = { param($value); $value.path_ownership.after_sha256 = $null; $value.path_ownership.after_value_kind = 'String' }; Pattern = 'PATH ownership' },
        @{ Name = 'added path without updates'; Change = { param($value); $value.path_ownership.installer_added = $true; $value.path_ownership.update_enabled = $false }; Pattern = 'PATH ownership' },
        @{ Name = 'pre-existing path from absent state'; Change = { param($value); $value.path_ownership.installer_added = $false; $value.path_ownership.update_enabled = $true; $value.path_ownership.before_state = 'absent'; $value.path_ownership.before_value_kind = $null }; Pattern = 'PATH ownership' },
        @{ Name = 'new path with ExpandString kind'; Change = { param($value); $value.path_ownership.after_value_kind = 'ExpandString' }; Pattern = 'PATH ownership' },
        @{ Name = 'option-like WSL distribution'; Change = { param($value); $value.wsl_distribution = '-x' }; Pattern = 'runtime authority|WSL distribution' }
    )) {
        $invalidManifest = ConvertTo-GoalRouterCanonicalJson $manifest | ConvertFrom-Json
        & $invalidControl.Change $invalidManifest
        $invalidJson = ConvertTo-GoalRouterCanonicalJson $invalidManifest
        Assert-Throws { Assert-GoalRouterInstalledManifestSchema -Manifest $invalidManifest -TrustedJson $invalidJson } $invalidControl.Pattern "launcher rejects $($invalidControl.Name)"
        Assert-Throws { Assert-GoalRouterExistingInstallManifest -Manifest $invalidManifest -Json $invalidJson -Layout $layout } $invalidControl.Pattern "installer/uninstaller rejects $($invalidControl.Name)"
    }
    $manifest.owned.launcher = 'D:\Outside\goalrouter.ps1'
    Assert-Throws { Assert-GoalRouterUninstallManifestLayout -Manifest $manifest } 'relationships' 'outside launcher relationship'
    Assert-Throws { Assert-GoalRouterInstalledManifestSchema -Manifest $manifest } 'relationships' 'launcher and uninstaller reject the same outside relationship'
    $directoryTarget = [pscustomobject]@{ Path = 'D:\Install\install.json'; ProviderName = 'FileSystem'; ProviderPath = 'D:\Install\install.json'; Exists = $true; IsContainer = $true; IsLeaf = $false; IsReparsePoint = $false; OwnerMatchesCurrentUser = $true; AclIsSafe = $true }
    Assert-Throws { Assert-GoalRouterUninstallFileTarget -Info $directoryTarget -ExpectedPath 'D:\Install\install.json' -AllowMissing $false } 'regular file' 'directory substituted for control leaf'
    $directoryTarget.IsContainer = $false; $directoryTarget.IsLeaf = $true; $directoryTarget.OwnerMatchesCurrentUser = $false
    Assert-Throws { Assert-GoalRouterUninstallFileTarget -Info $directoryTarget -ExpectedPath 'D:\Install\install.json' -AllowMissing $false } 'owned by' 'foreign-owned control leaf'
    $directoryTarget.OwnerMatchesCurrentUser = $true; $directoryTarget.IsReparsePoint = $true
    Assert-Throws { Assert-GoalRouterUninstallFileTarget -Info $directoryTarget -ExpectedPath 'D:\Install\install.json' -AllowMissing $false } 'reparse' 'reparse-point leaf is refused'
    $directoryTarget.IsReparsePoint = $false; $directoryTarget.ProviderPath = 'D:\Install\install.json:alternate'
    Assert-Throws { Assert-GoalRouterUninstallFileTarget -Info $directoryTarget -ExpectedPath 'D:\Install\install.json' -AllowMissing $false } 'provider|path' 'alternate data stream provider target is refused'
    $directoryTarget.ProviderPath = 'D:\Install\install.json'; $directoryTarget.AclIsSafe = $false
    Assert-Throws { Assert-GoalRouterUninstallFileTarget -Info $directoryTarget -ExpectedPath 'D:\Install\install.json' -AllowMissing $false } 'ACL' 'unsafe leaf ACL is refused'
    $source = [IO.File]::ReadAllText($installer)
    $fileRemoval = $source.Substring($source.IndexOf('$removeFile = {'), $source.IndexOf('$removeTree = {') - $source.IndexOf('$removeFile = {'))
    Assert-True (-not $fileRemoval.Contains('-Recurse')) 'expected file deletion is never recursive'
}

Invoke-Contract 'installed control canonicalization rejects reordered checksummed property input' {
    $layout = Get-GoalRouterWindowsLayout -LocalAppData 'C:\L' -AppData 'C:\R' -UserProfile 'C:\U' -InstallRoot 'D:\Install' -BinDir 'D:\Install\bin' -ConfigFile 'D:\Config\task-models.yaml' -StateDir 'D:\State' -CodexHome 'D:\Codex'
    $ownership = [pscustomobject]@{ InstallerAdded = $false; UpdateEnabled = $false; OwnedValue = $layout.BinDir; Before = [pscustomobject]@{ Present = $false; Value = $null }; After = [pscustomobject]@{ Present = $false; Value = $null } }
    $manifest = New-GoalRouterInstallManifest -Version '1.0.0' -ImageReference 'registry.example/router' -ImageDigest ('sha256:' + ('a' * 64)) -ImagePlatform 'linux/amd64' -SourceRevision 'rev' -WslDistribution 'Ubuntu' -Layout $layout -PathOwnership $ownership -ReleaseBase 'https://example.com/release'
    $fixedJson = ConvertTo-GoalRouterCanonicalJson $manifest
    $reordered = [ordered]@{
        release_base = $manifest.release_base; path_ownership = $manifest.path_ownership; wsl_distribution = $manifest.wsl_distribution; owned = $manifest.owned
        source_revision = $manifest.source_revision; image_platform = $manifest.image_platform; image_digest = $manifest.image_digest; image_reference = $manifest.image_reference
        launcher_version = $manifest.launcher_version; version = $manifest.version; protocol_version = $manifest.protocol_version; manifest_version = $manifest.manifest_version
    }
    $reorderedJson = $reordered | ConvertTo-Json -Compress -Depth 10
    Assert-True ($reorderedJson -cne $fixedJson) 'fixture property order differs while values remain equal'
    $parsed = $reorderedJson | ConvertFrom-Json
    Assert-Throws { Assert-GoalRouterExistingInstallManifest -Manifest $parsed -Json $reorderedJson -Layout $layout } 'canonical' 'installer/uninstaller reordered checksummed control'
    Assert-Throws { Assert-GoalRouterInstalledManifestSchema -Manifest $parsed -TrustedJson $reorderedJson } 'canonical' 'launcher reordered checksummed control'
}

Invoke-Contract 'uninstall commit retains recovery authority on failure and retry completes same scope' {
    $state = New-MemoryLifecycleState
    foreach ($path in @('C:\GoalRouter\bin\goalrouter.ps1', 'C:\GoalRouter\bin\goalrouter.cmd', 'C:\GoalRouter\bin\install.ps1', 'C:\GoalRouter\bin\uninstall.ps1', 'C:\GoalRouter\install.json')) { $state.Files[$path] = 'owned' }
    $state.ThrowPhase = 'remove:C:\GoalRouter\bin\goalrouter.cmd'
    $ports = New-MemoryLifecyclePorts -State $state
    $plan = [pscustomobject]@{
        Mode = 'preserve'
        RecoveryPath = 'C:\GoalRouter\uninstall-recovery.json'
        Manifest = [ordered]@{ owned = [ordered]@{ install_root = 'C:\GoalRouter' } }
        EarlyFiles = @('C:\GoalRouter\bin\goalrouter.ps1', 'C:\GoalRouter\bin\goalrouter.cmd')
        RemoveTrees = @()
        FinalFiles = @('C:\GoalRouter\install.json')
        InstallerPath = 'C:\GoalRouter\bin\install.ps1'
        UninstallerPath = 'C:\GoalRouter\bin\uninstall.ps1'
        PathResult = [pscustomobject]@{ Changed = $false; Snapshot = $state.UserPath }
    }
    Assert-Throws { Invoke-GoalRouterUninstallCommit -Plan $plan -Ports $ports } 'injected removal failure' 'injected uninstall failure'
    Assert-True $state.Files.ContainsKey('C:\GoalRouter\bin\uninstall.ps1') 'physical uninstaller retained'
    Assert-True $state.Files.ContainsKey('C:\GoalRouter\install.json') 'trusted control retained'
    Assert-True $state.Files.ContainsKey('C:\GoalRouter\uninstall-recovery.json') 'recovery mode retained'
    $state.ThrowPhase = $null
    Invoke-GoalRouterUninstallCommit -Plan $plan -Ports $ports
    foreach ($path in @('C:\GoalRouter\bin\goalrouter.ps1', 'C:\GoalRouter\bin\goalrouter.cmd', 'C:\GoalRouter\bin\install.ps1', 'C:\GoalRouter\bin\uninstall.ps1', 'C:\GoalRouter\install.json', 'C:\GoalRouter\uninstall-recovery.json')) { Assert-True (-not $state.Files.ContainsKey($path)) "removed $path" }
}

Invoke-Contract 'phase journal keeps every late uninstall phase retryable and attempts completed targets idempotently' {
    foreach ($failurePhase in @('set-user-path', 'remove:D:\State', 'remove:C:\GoalRouter\install.json', 'remove:C:\GoalRouter\bin\install.ps1', 'remove:C:\GoalRouter\bin\uninstall.ps1')) {
        $state = New-MemoryLifecycleState
        foreach ($path in @('C:\GoalRouter\bin\goalrouter.ps1', 'C:\GoalRouter\bin\goalrouter.cmd', 'C:\GoalRouter\bin\install.ps1', 'C:\GoalRouter\bin\uninstall.ps1', 'C:\GoalRouter\install.json', 'C:\GoalRouter\install.sha256', 'D:\State')) { $state.Files[$path] = 'owned' }
        $state.ThrowPhase = $failurePhase
        $ports = New-MemoryLifecyclePorts -State $state
        $manifest = [ordered]@{ owned = [ordered]@{ install_root = 'C:\GoalRouter' } }
        $plan = [pscustomobject]@{
            Mode = 'purge'; Manifest = $manifest
            RecoveryPath = 'C:\GoalRouter\uninstall-recovery.json'
            EarlyFiles = @('C:\GoalRouter\bin\goalrouter.ps1', 'C:\GoalRouter\bin\goalrouter.cmd')
            RemoveTrees = @('D:\State'); FinalFiles = @('C:\GoalRouter\install.json', 'C:\GoalRouter\install.sha256'); InstallerPath = 'C:\GoalRouter\bin\install.ps1'; UninstallerPath = 'C:\GoalRouter\bin\uninstall.ps1'
            PathResult = [pscustomobject]@{ Changed = $true; Snapshot = [pscustomobject]@{ Present = $false; Value = $null } }
        }
        Assert-Throws { Invoke-GoalRouterUninstallCommit -Plan $plan -Ports $ports } 'injected' "late failure $failurePhase"
        if ($failurePhase -cnotin @('remove:C:\GoalRouter\bin\install.ps1', 'remove:C:\GoalRouter\bin\uninstall.ps1')) { Assert-True $state.Files.ContainsKey('C:\GoalRouter\uninstall-recovery.json') "journal retained for $failurePhase" }
        if ($failurePhase -cne 'remove:C:\GoalRouter\bin\uninstall.ps1') { Assert-True $state.Files.ContainsKey('C:\GoalRouter\bin\uninstall.ps1') "uninstaller retained for $failurePhase" }
        $state.ThrowPhase = $null
        Invoke-GoalRouterUninstallCommit -Plan $plan -Ports $ports
        Assert-True (-not $state.Files.ContainsKey('C:\GoalRouter\uninstall-recovery.json')) "retry finishes $failurePhase"
    }
}

function New-FullInstallerFixture {
    $digest = 'sha256:' + ('a' * 64)
    $manifestJson = New-ReleaseManifestJson
    $manifestHash = '1' * 64
    $archiveHash = '2' * 64
    $fixture = [pscustomobject]@{
        Files = @{}
        Directories = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        Calls = [System.Collections.ArrayList]::new()
        Mutations = [System.Collections.ArrayList]::new()
        UserPath = [pscustomobject]@{ Present = $true; Value = 'C:\Tools' }
        ManifestJson = $manifestJson
        ManifestHash = $manifestHash
        ArchiveHash = $archiveHash
        Digest = $digest
        Version = '1.0.0'
        Revision = '0123456789abcdef'
        DoctorExitCode = 0
        FailDockerVersion = $false
        KernelOutput = @('5.15.153-microsoft-standard-WSL2')
        ThrowRemovePath = $null
        ResolveOverrides = @{}
        Removals = [System.Collections.ArrayList]::new()
    }
    foreach ($hostRoot in @('C:\Users\Me', 'C:\Users\Me\AppData\Roaming', 'C:\Users\Me\AppData\Local')) { [void]$fixture.Directories.Add($hostRoot) }
    $hostInfo = {
        return [pscustomobject]@{ LocalAppData = 'C:\Users\Me\AppData\Local'; AppData = 'C:\Users\Me\AppData\Roaming'; UserProfile = 'C:\Users\Me'; WindowsVersion = '10.0.22631'; PowerShellVersion = '5.1.22621.2506'; Platform = 'linux/amd64' }
    }.GetNewClosure()
    $resolve = {
        param([string]$Path, [string]$Kind, [bool]$AllowMissing)
        [void]$fixture.Calls.Add("resolve:${Kind}:$Path")
        if ($fixture.ResolveOverrides.ContainsKey($Path)) { return $fixture.ResolveOverrides[$Path] }
        $exists = $fixture.Files.ContainsKey($Path) -or $fixture.Directories.Contains($Path) -or $Path -in @('D:\Codex', 'D:\Project')
        return [pscustomobject]@{ Path = $Path; ProviderName = 'FileSystem'; ProviderPath = $Path; Exists = $exists; IsContainer = $Kind -ceq 'Directory'; IsLeaf = $Kind -ceq 'File'; IsReparsePoint = $false; ParentIsReparsePoint = $false; OwnerMatchesCurrentUser = $true; OwnerIsTrusted = $true; AclIsSafe = $true; AncestorChainIsSafe = $true }
    }.GetNewClosure()
    $newWork = { return 'C:\Temp\goalrouter-random-stage' }.GetNewClosure()
    $resolveLatest = { return [string]$fixture.Version }.GetNewClosure()
    $download = {
        param([string]$Uri, [string]$Destination, [bool]$AllowLoopbackHttp)
        [void]$fixture.Calls.Add("download:$Uri")
        $name = $Uri.Substring($Uri.LastIndexOf('/') + 1)
        if ($name -ceq 'SHA256SUMS') { $fixture.Files[$Destination] = "$($fixture.ManifestHash)  release-manifest.json`n$($fixture.ArchiveHash)  goalrouter-$($fixture.Version)-windows.zip`n" }
        elseif ($name -ceq 'release-manifest.json') { $fixture.Files[$Destination] = $fixture.ManifestJson }
        elseif ($name -ceq "goalrouter-$($fixture.Version)-windows.zip") { $fixture.Files[$Destination] = 'archive-bytes' }
        else { throw "unexpected download: $name" }
    }.GetNewClosure()
    $read = { param([string]$Path); if (-not $fixture.Files.ContainsKey($Path)) { throw "missing fake file: $Path" }; return [string]$fixture.Files[$Path] }.GetNewClosure()
    $write = { param([string]$Path, [string]$Content); $fixture.Files[$Path] = $Content }.GetNewClosure()
    $hash = {
        param([string]$Path)
        if ($Path.EndsWith('release-manifest.json')) { return $fixture.ManifestHash }
        if ($Path.EndsWith('windows.zip')) { return $fixture.ArchiveHash }
        throw "unexpected hash: $Path"
    }.GetNewClosure()
    $entries = {
        param([string]$Path)
        return @((New-ZipEntry 'goalrouter.ps1'), (New-ZipEntry 'goalrouter.cmd'), (New-ZipEntry 'install.ps1'), (New-ZipEntry 'uninstall.ps1'))
    }.GetNewClosure()
    $extract = {
        param([string]$Archive, [string]$Destination)
        foreach ($name in @('goalrouter.ps1', 'goalrouter.cmd', 'install.ps1', 'uninstall.ps1')) { $fixture.Files[(Join-GoalRouterWindowsPath $Destination $name)] = "staged-$($fixture.Version)-$name" }
    }.GetNewClosure()
    $native = {
        param([string]$FilePath, [string[]]$Arguments, [bool]$CaptureOutput)
        [void]$fixture.Calls.Add([pscustomobject]@{ Kind = 'native'; FilePath = $FilePath; Arguments = @($Arguments) })
        $joined = $Arguments -join ' '
        if ($joined -match ' docker version ') {
            if ($fixture.FailDockerVersion) { return [pscustomobject]@{ ExitCode = 7; Output = @('daemon unavailable') } }
            return [pscustomobject]@{ ExitCode = 0; Output = @('28.3.3 28.3.3') }
        }
        if ($joined -match ' uname -r') { return [pscustomobject]@{ ExitCode = 0; Output = $fixture.KernelOutput } }
        if ($joined -match ' wslinfo --wsl-version') { return [pscustomobject]@{ ExitCode = 0; Output = @('2.3.24') } }
        if ($joined -match ' docker info ') { return [pscustomobject]@{ ExitCode = 0; Output = @('x86_64') } }
        if ($joined -match ' docker pull ') { return [pscustomobject]@{ ExitCode = 0; Output = @() } }
        if ($joined -match '\.Architecture') { return [pscustomobject]@{ ExitCode = 0; Output = @('amd64') } }
        if ($joined -match 'RepoDigests') { return [pscustomobject]@{ ExitCode = 0; Output = @('ghcr.io/vparla/goalrouter@' + $fixture.Digest) } }
        if ($joined -match 'image\.revision') { return [pscustomobject]@{ ExitCode = 0; Output = @($fixture.Revision) } }
        if ($joined -match ' --json version') { return [pscustomobject]@{ ExitCode = 0; Output = @('{"version":"' + $fixture.Version + '","protocol_version":1}') } }
        if ($joined -match ' config template') { return [pscustomobject]@{ ExitCode = 0; Output = @('version: 1', 'tasks: {}') } }
        if ($joined -match ' config validate') { return [pscustomobject]@{ ExitCode = 0; Output = @() } }
        if ($joined -match ' wslpath ') { return [pscustomobject]@{ ExitCode = 0; Output = @('/mnt/d/fake-path') } }
        throw "unexpected native call: $joined"
    }.GetNewClosure()
    $snapshot = { param([string]$Path); if ($fixture.Files.ContainsKey($Path)) { return [pscustomobject]@{ Present = $true; Content = [string]$fixture.Files[$Path] } }; return [pscustomobject]@{ Present = $false; Content = $null } }.GetNewClosure()
    $replace = { param([string]$Path, [string]$Content); [void]$fixture.Mutations.Add("replace:$Path"); $fixture.Files[$Path] = $Content }.GetNewClosure()
    $restore = { param([string]$Path, $Snapshot); if ($Snapshot.Present) { $fixture.Files[$Path] = $Snapshot.Content } else { [void]$fixture.Files.Remove($Path) } }.GetNewClosure()
    $getPath = { return Copy-GoalRouterPathSnapshot $fixture.UserPath }.GetNewClosure()
    $setPath = { param($Snapshot); [void]$fixture.Mutations.Add('set-user-path'); $fixture.UserPath = Copy-GoalRouterPathSnapshot $Snapshot }.GetNewClosure()
    $doctor = { param([string]$FilePath, [string[]]$Arguments); [void]$fixture.Calls.Add([pscustomobject]@{ Kind = 'doctor'; FilePath = $FilePath; Arguments = @($Arguments) }); return $fixture.DoctorExitCode }.GetNewClosure()
    $ensure = {
        param([string]$Path)
        [void]$fixture.Mutations.Add("ensure:$Path")
        $existed = $fixture.Directories.Contains($Path) -or @($fixture.Files.Keys | Where-Object { $_.StartsWith($Path.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0
        [void]$fixture.Directories.Add($Path)
        if ($existed) { return @() }
        return @($Path)
    }.GetNewClosure()
    $remove = {
        param([string]$Path)
        if ([string]$fixture.ThrowRemovePath -ceq $Path) { throw "injected removal failure: $Path" }
        [void]$fixture.Removals.Add($Path)
        foreach ($key in @($fixture.Files.Keys)) { if ($key -ieq $Path -or $key.StartsWith($Path.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { [void]$fixture.Files.Remove($key) } }
        [void]$fixture.Directories.Remove($Path)
    }.GetNewClosure()
    $fixtureSentinelName = $script:GoalRouterDirectorySentinel
    $pathInfo = {
        param([string]$Path)
        $sentinelPath = Join-GoalRouterWindowsPath $Path $fixtureSentinelName
        $isLeaf = $fixture.Files.ContainsKey($Path)
        $entries = if ($isLeaf) { @() } else { @(@($fixture.Files.Keys) + @($fixture.Directories) | Where-Object { $_.StartsWith($Path.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) } | ForEach-Object { $_.Substring($Path.TrimEnd('\').Length + 1).Split('\')[0] } | Select-Object -Unique) }
        $isContainer = -not $isLeaf -and ($fixture.Directories.Contains($Path) -or $entries.Count -gt 0)
        return [pscustomobject]@{ Path = $Path; ProviderName = 'FileSystem'; ProviderPath = $Path; Exists = $isLeaf -or $isContainer; IsContainer = $isContainer; IsLeaf = $isLeaf; IsReparsePoint = $false; ContainsReparsePoint = $false; OwnerMatchesCurrentUser = $true; AclIsSafe = $true; Entries = $entries; Sentinel = if ($isContainer -and $fixture.Files.ContainsKey($sentinelPath)) { [string]$fixture.Files[$sentinelPath] } else { $null } }
    }.GetNewClosure()
    $ports = [pscustomobject]@{ GetHost = $hostInfo; ResolvePath = $resolve; ResolveLatestVersion = $resolveLatest; NewWorkDirectory = $newWork; Download = $download; ReadText = $read; WriteText = $write; GetHash = $hash; GetArchiveEntries = $entries; ExtractArchive = $extract; Native = $native; Snapshot = $snapshot; Replace = $replace; Restore = $restore; GetUserPath = $getPath; SetUserPath = $setPath; Doctor = $doctor; EnsureDirectory = $ensure; RemoveFile = $remove; RemoveTree = $remove; GetPathInfo = $pathInfo }
    return [pscustomobject]@{ State = $fixture; Ports = $ports }
}

function New-FullInstallOptions {
    return [pscustomobject]@{
        Version = '1.0.0'; InstallRoot = 'D:\Install'; BinDir = 'D:\Install\bin'; ConfigFile = 'D:\Config\task-models.yaml'; StateDir = 'D:\State'; CodexHome = 'D:\Codex'; WslDistribution = 'Ubuntu-24.04'
        Yes = $true; Force = $false; ResetConfig = $false; NoPathUpdate = $false; SkipDoctor = $false; SkipAccount = $true
        ReleaseBase = 'https://example.com/releases/v1.0.0'; AllowLoopbackHttp = $false; Image = 'ghcr.io/vparla/goalrouter:1.0.0'; AuthMode = 'existing-session'
    }
}

Invoke-Contract 'public install composition validates then installs custom layout with immutable parity' {
    $fixture = New-FullInstallerFixture
    Invoke-GoalRouterWindowsInstall -Options (New-FullInstallOptions) -Ports $fixture.Ports
    Assert-Equal $fixture.State.Files['D:\Install\bin\goalrouter.ps1'] 'staged-1.0.0-goalrouter.ps1' 'launcher installed'
    Assert-Equal $fixture.State.Files['D:\Install\bin\goalrouter.cmd'] 'staged-1.0.0-goalrouter.cmd' 'CMD shim installed'
    Assert-Equal $fixture.State.Files['D:\Install\install.json'] $fixture.State.Files['D:\State\install.json'] 'trusted/runtime parity exact'
    $control = $fixture.State.Files['D:\Install\install.json'] | ConvertFrom-Json
    Assert-Equal $control.wsl_distribution 'Ubuntu-24.04' 'selected WSL persisted'
    Assert-Equal $control.image_digest $fixture.State.Digest 'immutable digest persisted'
    Assert-Equal $control.owned.config_file 'D:\Config\task-models.yaml' 'custom config persisted'
    Assert-Equal $control.owned.codex_home 'D:\Codex' 'custom Codex persisted'
    Assert-True ('resolve:File:D:\Config\task-models.yaml' -cin @($fixture.State.Calls)) 'custom config file is provider-validated before mutation'
    Assert-Equal $fixture.State.UserPath.Value 'C:\Tools;D:\Install\bin' 'exact User PATH added'
    $firstMutation = @($fixture.State.Mutations)[0]
    Assert-True ($firstMutation -like 'ensure:*') 'product mutation starts only after preflight'
    $allCalls = @($fixture.State.Calls | ForEach-Object { if ($_ -is [string]) { $_ } else { $_.Arguments -join ' ' } }) -join "`n"
    Assert-True ($allCalls -match '-d Ubuntu-24.04 -- docker') 'selected WSL on every Docker call'
    Assert-True ($allCalls -notmatch 'OPENAI_API_KEY|Bearer |test-api-key') 'no credential material in calls'
}

Invoke-Contract 'trusted control never persists unrelated PATH secrets or private locations' {
    $fixture = New-FullInstallerFixture
    $originalPath = 'https://example.invalid/external-location;D:\private-repository;C:\Temp\secret-stage'
    $fixture.State.UserPath = [pscustomobject]@{ Present = $true; Value = $originalPath; ValueKind = 'ExpandString' }
    Invoke-GoalRouterWindowsInstall -Options (New-FullInstallOptions) -Ports $fixture.Ports
    foreach ($forbidden in @('external-location', 'private-repository', 'secret-stage', $originalPath)) {
        Assert-True (-not $fixture.State.Files['D:\Install\install.json'].Contains($forbidden)) "trusted control excludes PATH marker $forbidden"
        Assert-True (-not $fixture.State.Files['D:\State\install.json'].Contains($forbidden)) "runtime parity excludes PATH marker $forbidden"
    }
    Invoke-GoalRouterWindowsUninstall -SelectedInstallRoot 'D:\Install' -SelectedPurge $false -Confirmed $true -Ports $fixture.Ports -PhysicalUninstallerPath 'D:\Install\bin\uninstall.ps1'
    Assert-Equal $fixture.State.UserPath.Value $originalPath 'uninstall reconstructs exact unrelated PATH bytes'
    Assert-Equal $fixture.State.UserPath.ValueKind 'ExpandString' 'uninstall restores exact registry value kind'
}

Invoke-Contract 'public prerequisite failure occurs before product or User PATH mutation' {
    $fixture = New-FullInstallerFixture
    $fixture.State.FailDockerVersion = $true
    Assert-Throws { Invoke-GoalRouterWindowsInstall -Options (New-FullInstallOptions) -Ports $fixture.Ports } 'native prerequisite|Docker' 'Docker prerequisite failure'
    Assert-Equal $fixture.State.Mutations.Count 0 'no mutation before prerequisite success'
    Assert-Equal $fixture.State.UserPath.Value 'C:\Tools' 'User PATH unchanged'
}

Invoke-Contract 'unsafe host profile and AppData roots fail before native product or PATH mutation' {
    foreach ($case in @(
        @{ Path = 'C:\Users\Me'; Label = 'user profile' },
        @{ Path = 'C:\Users\Me\AppData\Roaming'; Label = 'roaming AppData' },
        @{ Path = 'C:\Users\Me\AppData\Local'; Label = 'local AppData' }
    )) {
        $fixture = New-FullInstallerFixture
        $fixture.State.ResolveOverrides[$case.Path] = [pscustomobject]@{ Path = $case.Path; ProviderName = 'FileSystem'; ProviderPath = $case.Path; Exists = $true; IsContainer = $true; IsLeaf = $false; IsReparsePoint = $true; ParentIsReparsePoint = $true; OwnerMatchesCurrentUser = $true; AclIsSafe = $true }
        Assert-Throws { Invoke-GoalRouterWindowsInstall -Options (New-FullInstallOptions) -Ports $fixture.Ports } 'reparse' "unsafe $($case.Label) root"
        Assert-Equal @($fixture.State.Calls | Where-Object { $_ -isnot [string] }).Count 0 "$($case.Label) rejects before native calls"
        Assert-Equal $fixture.State.Mutations.Count 0 "$($case.Label) rejects before product mutation"
        Assert-Equal $fixture.State.UserPath.Value 'C:\Tools' "$($case.Label) rejects before PATH mutation"
    }
}

Invoke-Contract 'trusted SYSTEM host root is accepted while an untrusted owner is rejected' {
    $fixture = New-FullInstallerFixture
    $trustedRoot = [pscustomobject]@{ Path = 'C:\Users\Me'; ProviderName = 'FileSystem'; ProviderPath = 'C:\Users\Me'; Exists = $true; IsContainer = $true; IsLeaf = $false; IsReparsePoint = $false; ParentIsReparsePoint = $false; OwnerMatchesCurrentUser = $false; OwnerIsTrusted = $true; AclIsSafe = $true; AncestorChainIsSafe = $true }
    $fixture.State.ResolveOverrides['C:\Users\Me'] = $trustedRoot
    Invoke-GoalRouterWindowsInstall -Options (New-FullInstallOptions) -Ports $fixture.Ports
    Assert-True (@($fixture.State.Calls | Where-Object { $_ -isnot [string] -and ($_.Arguments -join ' ') -match ' wslinfo --wsl-version' }).Count -eq 1) 'trusted SYSTEM root reaches WSL version prerequisite'
    $untrustedRoot = $trustedRoot.PSObject.Copy()
    $untrustedRoot.OwnerIsTrusted = $false
    $untrustedFixture = New-FullInstallerFixture
    $untrustedFixture.State.ResolveOverrides['C:\Users\Me'] = $untrustedRoot
    Assert-Throws { Invoke-GoalRouterWindowsInstall -Options (New-FullInstallOptions) -Ports $untrustedFixture.Ports } 'trusted principal' 'untrusted host root is rejected'
}

Invoke-Contract 'scalar WSL2 kernel output reaches later WSL and Docker prerequisites' {
    $fixture = New-FullInstallerFixture
    $fixture.State.KernelOutput = '5.15.153-microsoft-standard-WSL2'
    Invoke-GoalRouterWindowsInstall -Options (New-FullInstallOptions) -Ports $fixture.Ports
    $nativeCalls = @($fixture.State.Calls | Where-Object { $_ -isnot [string] })
    Assert-True (@($nativeCalls | Where-Object { ($_.Arguments -join ' ') -match ' wslinfo --wsl-version' }).Count -eq 1) 'scalar WSL2 output reaches WSL version prerequisite'
    Assert-True (@($nativeCalls | Where-Object { ($_.Arguments -join ' ') -match ' docker version ' }).Count -eq 1) 'scalar WSL2 output reaches Docker version prerequisite'
    foreach ($malformedOutput in @('not-a-kernel-WSL2', '5.15.153-microsoft-standard-WSL1', "5.15.153-microsoft-standard-WSL2`nextra", "5.15.153-microsoft-standard-WSL2`textra")) {
        $malformedFixture = New-FullInstallerFixture
        $malformedFixture.State.KernelOutput = $malformedOutput
        Assert-Throws { Invoke-GoalRouterWindowsInstall -Options (New-FullInstallOptions) -Ports $malformedFixture.Ports } 'not ready under WSL2' 'malformed kernel output is rejected'
    }
    $multipleLineFixture = New-FullInstallerFixture
    $multipleLineFixture.State.KernelOutput = @('5.15.153-microsoft-standard-WSL2', 'extra')
    Assert-Throws { Invoke-GoalRouterWindowsInstall -Options (New-FullInstallOptions) -Ports $multipleLineFixture.Ports } 'not ready under WSL2' 'multiple kernel output lines are rejected'
}

Invoke-Contract 'Win32-invalid and reserved custom destination components fail before native or mutation' {
    foreach ($badRoot in @('D:\Goal*Router', 'D:\Goal?Router', 'D:\Goal"Router', 'D:\Goal<Router', 'D:\Goal>Router', 'D:\Goal|Router', 'D:\CON\GoalRouter', 'D:\NUL.txt\GoalRouter', 'D:\COM1\GoalRouter', 'D:\LPT9.log\GoalRouter', 'D:\COM¹.txt\GoalRouter', 'D:\LPT³\GoalRouter', 'D:\CONIN$\GoalRouter', 'D:\CONOUT$.log\GoalRouter')) {
        $fixture = New-FullInstallerFixture
        $options = New-FullInstallOptions
        $options.InstallRoot = $badRoot
        $options.BinDir = Join-GoalRouterWindowsPath $badRoot 'bin'
        Assert-Throws { Invoke-GoalRouterWindowsInstall -Options $options -Ports $fixture.Ports } 'invalid|ambiguous|reserved|component' "invalid Win32 destination $badRoot"
        Assert-Equal @($fixture.State.Calls | Where-Object { $_ -isnot [string] }).Count 0 'invalid destination rejects before native calls'
        Assert-Equal $fixture.State.Mutations.Count 0 'invalid destination rejects before product mutation'
        Assert-Equal $fixture.State.UserPath.Value 'C:\Tools' 'invalid destination rejects before PATH mutation'
    }
}

Invoke-Contract 'unsafe higher existing destination ancestor fails before native or mutation' {
    $fixture = New-FullInstallerFixture
    $unsafe = [pscustomobject]@{ Path = 'D:\Install'; ProviderName = 'FileSystem'; ProviderPath = 'D:\Install'; Exists = $false; IsContainer = $false; IsLeaf = $false; IsReparsePoint = $false; ParentIsReparsePoint = $false; OwnerMatchesCurrentUser = $true; AclIsSafe = $true; AncestorOwnerMatchesCurrentUser = $true; AncestorAclIsSafe = $true; AncestorChainIsSafe = $false }
    $fixture.State.ResolveOverrides['D:\Install'] = $unsafe
    Assert-Throws { Invoke-GoalRouterWindowsInstall -Options (New-FullInstallOptions) -Ports $fixture.Ports } 'ancestor chain|ancestor.*unsafe' 'unsafe higher ancestor'
    Assert-Equal @($fixture.State.Calls | Where-Object { $_ -isnot [string] }).Count 0 'unsafe ancestor rejects before native calls'
    Assert-Equal $fixture.State.Mutations.Count 0 'unsafe ancestor rejects before product mutation'
    Assert-Equal $fixture.State.UserPath.Value 'C:\Tools' 'unsafe ancestor rejects before PATH mutation'
}

Invoke-Contract 'staging cleanup failure occurs before install transaction mutation' {
    $fixture = New-FullInstallerFixture
    $fixture.State.ThrowRemovePath = 'C:\Temp\goalrouter-random-stage'
    Assert-Throws { Invoke-GoalRouterWindowsInstall -Options (New-FullInstallOptions) -Ports $fixture.Ports } 'injected removal failure' 'staging cleanup failure'
    Assert-Equal $fixture.State.UserPath.Value 'C:\Tools' 'PATH unchanged after staging cleanup failure'
    Assert-True (-not $fixture.State.Files.ContainsKey('D:\Install\install.json')) 'trusted control not committed after staging cleanup failure'
    Assert-True (-not $fixture.State.Files.ContainsKey('D:\Install\bin\goalrouter.ps1')) 'launcher not committed after staging cleanup failure'

    $updateFixture = New-FullInstallerFixture
    $options = New-FullInstallOptions
    Invoke-GoalRouterWindowsInstall -Options $options -Ports $updateFixture.Ports
    $beforeControl = $updateFixture.State.Files['D:\Install\install.json']
    $beforeLauncher = $updateFixture.State.Files['D:\Install\bin\goalrouter.ps1']
    $beforePath = $updateFixture.State.UserPath.Value
    $updateFixture.State.ThrowRemovePath = 'C:\Temp\goalrouter-random-stage'
    $updateOptions = New-FullInstallOptions
    $updateOptions.Version = '1.0.1'
    $updateOptions.ReleaseBase = 'https://github.com/vparla/GoalRouter/releases/download/v1.0.1'
    $updateOptions.Image = 'ghcr.io/vparla/goalrouter:1.0.1'
    $updateFixture.State.Version = '1.0.1'
    $updateFixture.State.Revision = 'fedcba9876543210'
    $updateFixture.State.ManifestJson = New-ReleaseManifestJson -Version '1.0.1' -Image 'ghcr.io/vparla/goalrouter:1.0.1' -Revision $updateFixture.State.Revision
    Assert-Throws { Invoke-GoalRouterWindowsInstall -Options $updateOptions -Ports $updateFixture.Ports } 'injected removal failure' 'update staging cleanup failure'
    Assert-Equal $updateFixture.State.Files['D:\Install\install.json'] $beforeControl 'update cleanup failure preserves trusted control bytes'
    Assert-Equal $updateFixture.State.Files['D:\Install\bin\goalrouter.ps1'] $beforeLauncher 'update cleanup failure preserves launcher bytes'
    Assert-Equal $updateFixture.State.UserPath.Value $beforePath 'update cleanup failure preserves PATH'
}

Invoke-Contract 'reinstall retains original exact PATH ownership for later uninstall' {
    $fixture = New-FullInstallerFixture
    $options = New-FullInstallOptions
    Invoke-GoalRouterWindowsInstall -Options $options -Ports $fixture.Ports
    Invoke-GoalRouterWindowsInstall -Options $options -Ports $fixture.Ports
    $control = $fixture.State.Files['D:\Install\install.json'] | ConvertFrom-Json
    Assert-True $control.path_ownership.installer_added 'original PATH ownership retained'
    Assert-Equal $control.path_ownership.before_state 'populated' 'original PATH state classification retained'
    Assert-True (-not $fixture.State.Files['D:\Install\install.json'].Contains('C:\Tools')) 'unrelated PATH bytes are not persisted'
    Invoke-GoalRouterWindowsUninstall -SelectedInstallRoot 'D:\Install' -SelectedPurge $false -Confirmed $true -Ports $fixture.Ports -PhysicalUninstallerPath 'D:\Install\bin\uninstall.ps1'
    Assert-Equal $fixture.State.UserPath.Value 'C:\Tools' 'reinstall uninstall removes original owned entry'
}

Invoke-Contract 'preexisting bin PATH permits idempotent reinstall and update without false ownership' {
    $fixture = New-FullInstallerFixture
    $fixture.State.UserPath = [pscustomobject]@{ Present = $true; Value = 'C:\Tools;D:\Install\bin'; ValueKind = 'ExpandString' }
    $options = New-FullInstallOptions
    Invoke-GoalRouterWindowsInstall -Options $options -Ports $fixture.Ports
    Invoke-GoalRouterWindowsInstall -Options $options -Ports $fixture.Ports
    $control = $fixture.State.Files['D:\Install\install.json'] | ConvertFrom-Json
    Assert-True (-not [bool]$control.path_ownership.installer_added) 'preexisting bin is never claimed'
    Assert-True ([bool]$control.path_ownership.update_enabled) 'ordinary update policy remains enabled'
    $fixture.State.Version = '1.0.1'; $fixture.State.Revision = 'revision-1.0.1'; $fixture.State.ManifestJson = New-ReleaseManifestJson -Version '1.0.1' -Image 'ghcr.io/vparla/goalrouter:1.0.1' -Revision $fixture.State.Revision
    $options.Version = '1.0.1'; $options.Image = 'ghcr.io/vparla/goalrouter:1.0.1'; $options.ReleaseBase = 'https://github.com/vparla/GoalRouter/releases/download/v1.0.1'
    Invoke-GoalRouterWindowsInstall -Options $options -Ports $fixture.Ports
    Assert-Equal (($fixture.State.Files['D:\Install\install.json'] | ConvertFrom-Json).version) '1.0.1' 'preexisting PATH update succeeds'
    Assert-Equal $fixture.State.UserPath.Value 'C:\Tools;D:\Install\bin' 'preexisting PATH bytes remain exact'
    Assert-Equal $fixture.State.UserPath.ValueKind 'ExpandString' 'preexisting PATH kind remains exact'
}

Invoke-Contract 'corrupt existing control requires Force before any native or product mutation' {
    $fixture = New-FullInstallerFixture
    $fixture.State.Files['D:\Install\install.json'] = 'corrupt-control'
    $options = New-FullInstallOptions
    Assert-Throws { Invoke-GoalRouterWindowsInstall -Options $options -Ports $fixture.Ports } 'corrupt.*-Force|-Force.*corrupt' 'corrupt control refusal'
    Assert-Equal @($fixture.State.Calls | Where-Object { $_ -isnot [string] -and $_.Kind -ceq 'native' }).Count 0 'no native call before corrupt-control refusal'
    Assert-Equal $fixture.State.Mutations.Count 0 'no mutation before corrupt-control refusal'
    $options.Force = $true
    $priorErrorCount = $Error.Count
    Invoke-GoalRouterWindowsInstall -Options $options -Ports $fixture.Ports
    Assert-Equal $Error.Count $priorErrorCount 'Force schema repair leaves no swallowed ErrorRecord'
    Assert-True ($fixture.State.Files['D:\Install\install.json'] -ne 'corrupt-control') 'Force repairs control'
}

Invoke-Contract 'checksummed noncanonical existing control requires explicit Force repair before native or mutation' {
    $fixture = New-FullInstallerFixture
    $options = New-FullInstallOptions
    Invoke-GoalRouterWindowsInstall -Options $options -Ports $fixture.Ports
    $record = $fixture.State.Files['D:\Install\install.json'] | ConvertFrom-Json
    $record | Add-Member -NotePropertyName injected_authority -NotePropertyValue 'D:\Outside'
    $forgedJson = ConvertTo-GoalRouterCanonicalJson $record
    $forgedChecksum = (Get-GoalRouterStringSha256 $forgedJson) + "`n"
    $fixture.State.Files['D:\Install\install.json'] = $forgedJson
    $fixture.State.Files['D:\Install\install.sha256'] = $forgedChecksum
    $fixture.State.Files['D:\State\install.json'] = $forgedJson
    $fixture.State.Files['D:\State\install.sha256'] = $forgedChecksum
    $fixture.State.Calls.Clear(); $fixture.State.Mutations.Clear()
    $beforePath = $fixture.State.UserPath.Value
    Assert-Throws { Invoke-GoalRouterWindowsInstall -Options $options -Ports $fixture.Ports } 'corrupt.*-Force|schema|canonical' 'semantic existing-control refusal'
    Assert-Equal @($fixture.State.Calls | Where-Object { $_ -isnot [string] }).Count 0 'semantic control fails before native calls'
    Assert-Equal $fixture.State.Mutations.Count 0 'semantic control fails before product mutation'
    Assert-Equal $fixture.State.UserPath.Value $beforePath 'semantic control fails before PATH mutation'
    $options.Force = $true
    Invoke-GoalRouterWindowsInstall -Options $options -Ports $fixture.Ports
    Assert-True (-not (($fixture.State.Files['D:\Install\install.json'] | ConvertFrom-Json).PSObject.Properties.Name -contains 'injected_authority')) 'Force replaces noncanonical authority'
}

Invoke-Contract 'direct update fails closed on missing or forged runtime state parity' {
    foreach ($mode in @('missing', 'forged')) {
        $fixture = New-FullInstallerFixture
        $options = New-FullInstallOptions
        Invoke-GoalRouterWindowsInstall -Options $options -Ports $fixture.Ports
        if ($mode -ceq 'missing') { [void]$fixture.State.Files.Remove('D:\State\install.json') }
        else { $fixture.State.Files['D:\State\install.json'] = '{"forged":true}' }
        $fixture.State.Calls.Clear()
        $fixture.State.Mutations.Clear()
        Assert-Throws { Invoke-GoalRouterWindowsInstall -Options $options -Ports $fixture.Ports } 'state.*parity|parity.*state' "$mode runtime state parity"
        Assert-Equal $fixture.State.Mutations.Count 0 "$mode parity has no product or PATH mutation"
        Assert-True (@($fixture.State.Calls | Where-Object { $_ -isnot [string] }).Count -eq 0) "$mode parity fails before native calls"
    }
}

Invoke-Contract 'unsafe image override fails before native or product mutation' {
    $fixture = New-FullInstallerFixture
    $options = New-FullInstallOptions
    $options.Image = '--privileged'
    Assert-Throws { Invoke-GoalRouterWindowsInstall -Options $options -Ports $fixture.Ports } 'image' 'unsafe image override'
    Assert-Equal @($fixture.State.Calls | Where-Object { $_ -isnot [string] -and $_.Kind -ceq 'native' }).Count 0 'no native image call'
    Assert-Equal $fixture.State.Mutations.Count 0 'no product mutation'
}

Invoke-Contract 'public preserve uninstall removes lifecycle control parity and owned PATH only' {
    $fixture = New-FullInstallerFixture
    Invoke-GoalRouterWindowsInstall -Options (New-FullInstallOptions) -Ports $fixture.Ports
    $fixture.State.Files['D:\State\runs\keep.json'] = 'durable-state'
    Invoke-GoalRouterWindowsUninstall -SelectedInstallRoot 'D:\Install' -SelectedPurge $false -Confirmed $true -Ports $fixture.Ports -PhysicalUninstallerPath 'D:\Install\bin\uninstall.ps1'
    foreach ($path in @('D:\Install\bin\goalrouter.ps1', 'D:\Install\bin\goalrouter.cmd', 'D:\Install\bin\install.ps1', 'D:\Install\bin\uninstall.ps1', 'D:\Install\install.json', 'D:\State\install.json')) { Assert-True (-not $fixture.State.Files.ContainsKey($path)) "preserve removes $path" }
    Assert-True $fixture.State.Files.ContainsKey('D:\Config\task-models.yaml') 'configuration preserved'
    Assert-Equal $fixture.State.Files['D:\State\runs\keep.json'] 'durable-state' 'durable state preserved'
    Assert-Equal $fixture.State.UserPath.Value 'C:\Tools' 'exact owned PATH restored'
}

Invoke-Contract 'initial uninstall refuses missing or corrupt install-root ownership sentinel before mutation' {
    foreach ($sentinelValue in @($null, 'foreign-owner')) {
        $fixture = New-FullInstallerFixture
        Invoke-GoalRouterWindowsInstall -Options (New-FullInstallOptions) -Ports $fixture.Ports
        if ($null -eq $sentinelValue) { [void]$fixture.State.Files.Remove('D:\Install\.goalrouter-owned-v1') }
        else { $fixture.State.Files['D:\Install\.goalrouter-owned-v1'] = $sentinelValue }
        $beforeControl = $fixture.State.Files['D:\Install\install.json']
        $beforePath = $fixture.State.UserPath.Value
        Assert-Throws { Invoke-GoalRouterWindowsUninstall -SelectedInstallRoot 'D:\Install' -SelectedPurge $false -Confirmed $true -Ports $fixture.Ports -PhysicalUninstallerPath 'D:\Install\bin\uninstall.ps1' } 'install root ownership sentinel' "root sentinel $sentinelValue"
        Assert-Equal $fixture.State.Files['D:\Install\install.json'] $beforeControl 'sentinel refusal preserves trusted control'
        Assert-Equal $fixture.State.UserPath.Value $beforePath 'sentinel refusal preserves PATH'
    }
}

Invoke-Contract 'uninstall proves host roots before purge guard control reads or mutation' {
    foreach ($hostRoot in @('C:\Users\Me', 'C:\Users\Me\AppData\Roaming', 'C:\Users\Me\AppData\Local')) {
        $fixture = New-FullInstallerFixture
        Invoke-GoalRouterWindowsInstall -Options (New-FullInstallOptions) -Ports $fixture.Ports
        $fixture.State.ResolveOverrides[$hostRoot] = [pscustomobject]@{ Path = $hostRoot; ProviderName = 'FileSystem'; ProviderPath = 'D:\Redirected'; Exists = $true; IsContainer = $true; IsLeaf = $false; IsReparsePoint = $true; ParentIsReparsePoint = $true; OwnerMatchesCurrentUser = $false; AclIsSafe = $false; AncestorChainIsSafe = $false }
        $fixture.State.Removals.Clear(); $fixture.State.Mutations.Clear()
        $beforePath = $fixture.State.UserPath.Value
        Assert-Throws { Invoke-GoalRouterWindowsUninstall -SelectedInstallRoot 'D:\Install' -SelectedPurge $true -Confirmed $true -Ports $fixture.Ports -PhysicalUninstallerPath 'D:\Install\bin\uninstall.ps1' } 'profile|AppData|provider|reparse|owned|ACL' "unsafe uninstall host root $hostRoot"
        Assert-Equal $fixture.State.Removals.Count 0 'unsafe uninstall host root rejects before any deletion'
        Assert-Equal $fixture.State.Mutations.Count 0 'unsafe uninstall host root rejects before replacement mutation'
        Assert-Equal $fixture.State.UserPath.Value $beforePath 'unsafe uninstall host root rejects before PATH mutation'
    }
}

Invoke-Contract 'public purge removes only exact trusted config and state trees' {
    $fixture = New-FullInstallerFixture
    Invoke-GoalRouterWindowsInstall -Options (New-FullInstallOptions) -Ports $fixture.Ports
    $fixture.State.Files['D:\State\runs\keep.json'] = 'purged-state'
    $fixture.State.Files['D:\Outside\keep.txt'] = 'outside'
    Invoke-GoalRouterWindowsUninstall -SelectedInstallRoot 'D:\Install' -SelectedPurge $true -Confirmed $true -Ports $fixture.Ports -PhysicalUninstallerPath 'D:\Install\bin\uninstall.ps1'
    Assert-True (-not (@($fixture.State.Files.Keys | Where-Object { $_.StartsWith('D:\State\', [StringComparison]::OrdinalIgnoreCase) }).Count)) 'state tree purged'
    Assert-True (-not (@($fixture.State.Files.Keys | Where-Object { $_.StartsWith('D:\Config\', [StringComparison]::OrdinalIgnoreCase) }).Count)) 'config tree purged'
    Assert-Equal $fixture.State.Files['D:\Outside\keep.txt'] 'outside' 'outside data preserved'
}

Invoke-Contract 'preserve and purge uninstall both permit bounded fresh reinstall' {
    foreach ($purge in @($false, $true)) {
        $fixture = New-FullInstallerFixture
        $options = New-FullInstallOptions
        Invoke-GoalRouterWindowsInstall -Options $options -Ports $fixture.Ports
        $fixture.State.Files['D:\Config\task-models.yaml'] = 'user-config'
        Invoke-GoalRouterWindowsUninstall -SelectedInstallRoot 'D:\Install' -SelectedPurge $purge -Confirmed $true -Ports $fixture.Ports -PhysicalUninstallerPath 'D:\Install\bin\uninstall.ps1'
        if (-not $purge) {
            Assert-True $fixture.State.Files.ContainsKey('D:\Install\.goalrouter-owned-v1') 'preserve retains install-root sentinel'
            Assert-True $fixture.State.Files.ContainsKey('D:\Config\.goalrouter-owned-v1') 'preserve retains config sentinel'
            Assert-True $fixture.State.Files.ContainsKey('D:\State\.goalrouter-owned-v1') 'preserve retains state sentinel'
        }
        Invoke-GoalRouterWindowsInstall -Options $options -Ports $fixture.Ports
        Assert-True $fixture.State.Files.ContainsKey('D:\Install\install.json') "fresh reinstall after purge=$purge"
        if (-not $purge) { Assert-Equal $fixture.State.Files['D:\Config\task-models.yaml'] 'user-config' 'preserved config survives reinstall' }
    }
}

Invoke-Contract 'installed lifecycle dispatch uses powershell and never WSL or runtime state' {
    $layout = Get-GoalRouterWindowsLayout -LocalAppData 'C:\L' -AppData 'C:\R' -UserProfile 'C:\U' -InstallRoot 'D:\Install' -BinDir 'D:\Install\bin' -ConfigFile 'E:\Cfg\task-models.yaml' -StateDir 'E:\State' -CodexHome 'E:\Codex'
    $manifest = New-GoalRouterInstallManifest -Version '1.0.0' -ImageReference 'ghcr.io/vparla/goalrouter' -ImageDigest ('sha256:' + ('a' * 64)) -ImagePlatform 'linux/amd64' -SourceRevision '0123456789abcdef' -WslDistribution 'Ubuntu-Trusted' -Layout $layout -PathOwnership ([pscustomobject]@{ InstallerAdded = $false; OwnedValue = $layout.BinDir; Before = [pscustomobject]@{ Present = $false; Value = $null }; After = [pscustomobject]@{ Present = $false; Value = $null } }) -ReleaseBase 'https://example.com/release'
    $calls = [System.Collections.ArrayList]::new()
    $script:GoalRouterNativeInvoker = {
        param([string]$FilePath, [string[]]$Arguments, [bool]$CaptureOutput)
        [void]$calls.Add([pscustomobject]@{ FilePath = $FilePath; Arguments = @($Arguments) })
        return [pscustomobject]@{ ExitCode = 17; Output = @() }
    }.GetNewClosure()
    $status = Invoke-GoalRouterInstalledLifecycle -Command 'update' -CommandArguments @('1.0.1') -Manifest $manifest -PhysicalLauncherPath $layout.LauncherPath
    Assert-Equal $status 17 'trusted lifecycle status'
    Assert-Equal $calls.Count 1 'one lifecycle process'
    Assert-Equal $calls[0].FilePath 'powershell.exe' 'Windows PowerShell lifecycle host'
    Assert-Equal $calls[0].Arguments[0..5] @('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $layout.InstallerPath) 'physical installer prefix'
    Assert-True ($calls[0].Arguments -notcontains 'wsl.exe') 'no WSL lifecycle fallback'
    Assert-True (($calls[0].Arguments -join ' ') -notmatch 'Evil-Distro|evil.example|OPENAI_API_KEY') 'runtime state and secrets absent'
}

Invoke-Contract 'installed doctor options are strict and remain launcher-owned' {
    Assert-True (-not (ConvertFrom-GoalRouterDoctorArguments -Arguments @())) 'default doctor checks account'
    Assert-True (ConvertFrom-GoalRouterDoctorArguments -Arguments @('-SkipAccount')) 'SkipAccount is parsed by launcher'
    Assert-Throws { ConvertFrom-GoalRouterDoctorArguments -Arguments @('-SkipAccount', '-SkipAccount') } 'duplicate' 'duplicate SkipAccount'
    Assert-Throws { ConvertFrom-GoalRouterDoctorArguments -Arguments @('--skip-account') } 'invalid' 'no compatibility alias'
}

Invoke-Contract 'installed doctor and version bind trusted paths and readonly authority' {
    $trusted = [pscustomobject]@{ owned = [pscustomobject]@{ config_file = 'D:\Config\task-models.yaml'; state_dir = 'D:\State'; codex_home = 'D:\Codex' } }
    $parsed = [pscustomobject]@{ Config = $null; StateDir = $null; CodexHome = $null }
    $selected = Get-GoalRouterTrustedMaintenanceInputs -Parsed $parsed -TrustedInstall $trusted
    Assert-Equal $selected.Config 'D:\Config\task-models.yaml' 'trusted maintenance config'
    foreach ($field in @('Config', 'StateDir', 'CodexHome')) {
        $candidate = [pscustomobject]@{ Config = $null; StateDir = $null; CodexHome = $null }
        $candidate.$field = 'E:\Foreign'
        Assert-Throws { Get-GoalRouterTrustedMaintenanceInputs -Parsed $candidate -TrustedInstall $trusted } 'explicit.*trusted' "foreign maintenance $field"
    }
    $source = [IO.File]::ReadAllText($launcher)
    Assert-True $source.Contains("installed doctor and version require readonly access") 'maintenance rejects elevated access'
    Assert-True ($source.Contains("Access = if (`$RequireTrustedMaintenance) { 'readonly' }") -and $source.Contains('$requiresTrustedMaintenance')) 'maintenance context forces readonly Docker authority'
}

Invoke-Contract 'installed version record binds trusted control to verified runtime query' {
    $layout = Get-GoalRouterWindowsLayout -LocalAppData 'C:\L' -AppData 'C:\R' -UserProfile 'C:\U'
    $manifest = New-GoalRouterInstallManifest -Version '1.0.0' -ImageReference 'ghcr.io/vparla/goalrouter' -ImageDigest ('sha256:' + ('a' * 64)) -ImagePlatform 'linux/amd64' -SourceRevision '0123456789abcdef' -WslDistribution 'Ubuntu-Trusted' -Layout $layout -PathOwnership ([pscustomobject]@{ InstallerAdded = $false; OwnedValue = $layout.BinDir; Before = [pscustomobject]@{ Present = $false; Value = $null }; After = [pscustomobject]@{ Present = $false; Value = $null } }) -ReleaseBase 'https://example.com/release'
    $record = New-GoalRouterTrustedVersionRecord -Manifest $manifest -RuntimeJson '{"version":"1.0.0","protocol_version":1,"revision":"0123456789abcdef"}'
    Assert-Equal $record.launcher_version '1.0.0' 'trusted launcher version'
    Assert-Equal $record.protocol_version 1 'trusted protocol'
    Assert-Equal $record.image_digest ('sha256:' + ('a' * 64)) 'trusted image digest'
    Assert-Equal $record.source_revision '0123456789abcdef' 'trusted revision'
    Assert-Equal $record.image_platform 'linux/amd64' 'trusted platform'
    Assert-Equal $record.wsl_distribution 'Ubuntu-Trusted' 'trusted WSL'
    Assert-Equal $record.runtime.version '1.0.0' 'verified runtime version'
    Assert-Throws { New-GoalRouterTrustedVersionRecord -Manifest $manifest -RuntimeJson '{"version":"9.9.9","protocol_version":1}' } 'runtime version' 'runtime drift'
}

Invoke-Contract 'launcher source proves control-first installed maintenance order' {
    $source = [IO.File]::ReadAllText($launcher)
    foreach ($required in @('Get-GoalRouterPhysicalInstallControl', 'Assert-GoalRouterTrustedStateParity', 'Invoke-GoalRouterInstalledLifecycle')) { Assert-True $source.Contains($required) "launcher contains $required" }
    foreach ($proof in @('Assert-GoalRouterTrustedPhysicalLeaf', 'Assert-GoalRouterTrustedPhysicalDirectory', "-Label 'trusted install control checksum'", "-Label 'runtime state parity checksum'", "-Label 'trusted installer'", "-Label 'trusted uninstaller'", "-Label 'install root'")) { Assert-True $source.Contains($proof) "launcher contains physical proof $proof" }
    Assert-True $source.Contains('Assert-GoalRouterInstalledManifestSchema') 'launcher validates exact nested manifest schema'
    $entryStart = $source.IndexOf('function Invoke-GoalRouterLauncher')
    $entryText = $source.Substring($entryStart)
    $controlIndex = $entryText.IndexOf('Get-GoalRouterPhysicalInstallControl')
    $contextIndex = $entryText.IndexOf('New-GoalRouterContext')
    Assert-True ($controlIndex -ge 0 -and $contextIndex -gt $controlIndex) 'trusted control is discovered before ordinary context/state'
    Assert-True ($entryText.Contains("-ceq 'update'") -and $entryText.Contains("-ceq 'uninstall'")) 'host maintenance dispatch is explicit and case-sensitive'
    Assert-True $source.Contains("(`$maintenanceCommand -ceq 'doctor' -or `$maintenanceCommand -ceq 'version')") 'readonly maintenance dispatch is explicit and case-sensitive'
}

Invoke-Contract 'production download and control hashing are explicit and auditable' {
    $installerSource = [IO.File]::ReadAllText($installer)
    $launcherSource = [IO.File]::ReadAllText($launcher)
    Assert-True $installerSource.Contains('AllowAutoRedirect = $false') 'redirects are manually validated'
    Assert-True $installerSource.Contains('release download failed') 'download errors are URL-free'
    Assert-True $installerSource.Contains('install.sha256') 'trusted control checksum is installed'
    Assert-True $launcherSource.Contains('Get-FileHash') 'launcher verifies trusted control checksum'
    Assert-True $installerSource.Contains('SendMessageTimeoutW') 'User PATH update broadcast uses Win32 API'
    Assert-True $installerSource.Contains('WM_SETTINGCHANGE') 'User PATH broadcast targets environment settings'
}

Invoke-Contract 'direct lifecycle parameter binding rejects duplicate unknown and incomplete options' {
    foreach ($case in @(
        @{ Script = $installer; Args = @('-Force', '-Force'); Pattern = 'specified more than once|parameter' },
        @{ Script = $installer; Args = @('-Unknown'); Pattern = 'cannot be found|parameter' },
        @{ Script = $installer; Args = @('-Version'); Pattern = 'missing an argument|parameter' },
        @{ Script = $uninstaller; Args = @('-Purge', '-Purge'); Pattern = 'specified more than once|parameter' }
    )) {
        $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
        $startInfo.FileName = 'pwsh'
        $startInfo.UseShellExecute = $false
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        [void]$startInfo.ArgumentList.Add('-NoLogo'); [void]$startInfo.ArgumentList.Add('-NoProfile'); [void]$startInfo.ArgumentList.Add('-File'); [void]$startInfo.ArgumentList.Add($case.Script)
        foreach ($argument in $case.Args) { [void]$startInfo.ArgumentList.Add($argument) }
        $process = [System.Diagnostics.Process]::Start($startInfo)
        $stdout = $process.StandardOutput.ReadToEnd(); $stderr = $process.StandardError.ReadToEnd(); $process.WaitForExit()
        Assert-True ($process.ExitCode -ne 0) 'invalid parameter invocation fails'
        Assert-True (($stdout + $stderr) -match $case.Pattern) 'binding error is explicit'
    }
}

Invoke-Contract 'lifecycle production scripts are strict 5.1 and avoid command strings' {
    foreach ($path in @($installer, $uninstaller, $launcher)) {
        $tokens = $null
        $parseErrors = $null
        [void][System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$parseErrors)
        Assert-Equal @($parseErrors).Count 0 "parser errors for $path"
        $source = [System.IO.File]::ReadAllText($path)
        foreach ($forbidden in @('??', '?.', 'ForEach-Object -Parallel', '$IsWindows', 'Invoke-Expression')) {
            Assert-True (-not $source.Contains($forbidden)) "forbidden syntax $forbidden in $path"
        }
        Assert-True ($source.Contains('Set-StrictMode -Version Latest')) "strict mode in $path"
        Assert-True ($source.Contains("`$ErrorActionPreference = 'Stop'")) "stop-on-error in $path"
        Assert-True ($source.Contains("`$WarningPreference = 'Stop'")) "stop-on-warning in $path"
    }
    $expected = "@echo off`npowershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File `"%~dp0goalrouter.ps1`" %*`nexit /b %ERRORLEVEL%`n"
    Assert-Equal ([System.IO.File]::ReadAllText($shim)) $expected 'CMD shim remains exact'
}

Invoke-Contract 'installed doctor is launcher-owned and never forwards a nonexistent runtime doctor command' {
    $calls = [System.Collections.ArrayList]::new()
    $script:GoalRouterNativeInvoker = {
        param([string]$FilePath, [string[]]$Arguments, [bool]$CaptureOutput)
        [void]$calls.Add(($Arguments -join ' '))
        return [pscustomobject]@{ ExitCode = 0; Output = @() }
    }.GetNewClosure()
    $context = [pscustomobject]@{ Distribution = 'Ubuntu-Trusted'; Image = 'registry.example/goalrouter@sha256:' + ('a' * 64); Config = '/mnt/d/config.yaml'; State = '/mnt/d/state'; Project = '/mnt/d/project'; CodexHome = $null; AuthMode = 'api-key'; Access = 'readonly'; Json = $false; Forwarded = @() }
    $env:OPENAI_API_KEY = 'doctor-fixture-key'
    try { Assert-Equal (Invoke-GoalRouterInstalledDoctor -Context $context -SkipAccount $true) 0 'launcher doctor status' }
    finally { Remove-Item Env:OPENAI_API_KEY }
    $joined = @($calls) -join "`n"
    Assert-True ($joined -match 'docker version') 'doctor checks daemon'
    Assert-True ($joined -match 'docker image inspect') 'doctor checks immutable image'
    Assert-True ($joined -match 'config validate') 'doctor validates mounted configuration'
    Assert-True ($joined -notmatch '(?:^| )doctor(?: |$)') 'doctor is not sent to the Python CLI'
}

Invoke-Contract 'install destinations reject wrong kinds foreign content and protected parents before mutation' {
    $safeMissing = [pscustomobject]@{ Path = 'D:\Safe'; ProviderName = 'FileSystem'; ProviderPath = 'D:\Safe'; Exists = $false; IsContainer = $false; IsLeaf = $false; IsReparsePoint = $false; ParentIsReparsePoint = $false; Entries = @() }
    Assert-GoalRouterLifecyclePathInfo -Info $safeMissing -Label 'safe' -AllowMissing $true -ProtectedRoots @('C:\Users\Me')
    $wrongKind = $safeMissing.PSObject.Copy(); $wrongKind.Exists = $true; $wrongKind.IsLeaf = $true
    Assert-Throws { Assert-GoalRouterLifecyclePathInfo -Info $wrongKind -Label 'directory' -AllowMissing $true -ProtectedRoots @('C:\Users\Me') -RequiredKind 'Directory' } 'directory|kind' 'file cannot stand in for directory'
    $foreign = $safeMissing.PSObject.Copy(); $foreign.Exists = $true; $foreign.IsContainer = $true; $foreign.Entries = @('foreign.txt')
    Assert-Throws { Assert-GoalRouterInstallDestination -Info $foreign -Label 'state directory' -AllowedEntries @() } 'foreign|nonempty|owned' 'foreign nonempty directory is not adopted'
    $parent = $safeMissing.PSObject.Copy(); $parent.Path = 'C:\Users'; $parent.ProviderPath = $parent.Path
    Assert-Throws { Assert-GoalRouterLifecyclePathInfo -Info $parent -Label 'parent' -AllowMissing $true -ProtectedRoots @('C:\Users\Me') } 'protected' 'parents of protected roots are refused'
}

Invoke-Contract 'uninstall recovery journal carries trusted authority and permits completed purge targets' {
    $manifest = [ordered]@{ manifest_version = 1; protocol_version = 1; owned = [ordered]@{ install_root = 'D:\Install'; config_dir = 'D:\Config'; config_file = 'D:\Config\task-models.yaml'; state_dir = 'D:\State'; launcher = 'D:\Install\bin\goalrouter.ps1'; cmd = 'D:\Install\bin\goalrouter.cmd'; installer = 'D:\Install\bin\install.ps1'; uninstaller = 'D:\Install\bin\uninstall.ps1' }; path_ownership = [ordered]@{ installer_added = $false; update_enabled = $false; owned_value = 'D:\Install\bin'; before_state = 'absent'; before_value_kind = $null; after_value_kind = $null; after_sha256 = $null } }
    $record = New-GoalRouterUninstallRecoveryRecord -Mode 'purge' -Phase 'trees' -Manifest $manifest
    Assert-Equal $record.mode 'purge' 'journal mode'
    Assert-Equal $record.phase 'trees' 'journal phase'
    Assert-Equal $record.manifest.owned.install_root 'D:\Install' 'journal retains authority'
    $missing = New-SafePathInfo -Path 'D:\Config'; $missing.Exists = $false; $missing.IsContainer = $false; $missing.Sentinel = $null
    $present = New-SafePathInfo -Path 'D:\State'
    $plan = New-GoalRouterUninstallPlan -Manifest $manifest -Purge $true -PathInfos @{ Config = $missing; State = $present } -CurrentUserPath ([pscustomobject]@{ Present = $false; Value = $null }) -RecoveryMode 'purge'
    Assert-Equal $plan.RemoveTrees @('D:\State') 'retry skips already-completed tree'
}

Invoke-Contract 'update preserves canonical transport custom image and no-PATH policy' {
    $layout = Get-GoalRouterWindowsLayout -LocalAppData 'C:\L' -AppData 'C:\R' -UserProfile 'C:\U' -InstallRoot 'D:\Install' -BinDir 'D:\Install\bin'
    $ownership = [pscustomobject]@{ InstallerAdded = $false; OwnedValue = $layout.BinDir; Before = [pscustomobject]@{ Present = $true; Value = 'C:\Tools' }; After = [pscustomobject]@{ Present = $true; Value = 'C:\Tools' } }
    $manifest = New-GoalRouterInstallManifest -Version '1.0.0' -ImageReference 'registry.example/custom/router' -ImageDigest ('sha256:' + ('a' * 64)) -ImagePlatform 'linux/amd64' -SourceRevision 'rev' -WslDistribution 'Ubuntu' -Layout $layout -PathOwnership $ownership -ReleaseBase 'https://github.com/vparla/GoalRouter/releases/download/v1.0.0'
    $invocation = New-GoalRouterMaintenanceInvocation -Command update -CommandArguments @('1.2.3') -Manifest $manifest -PhysicalLauncherPath $layout.LauncherPath
    $args = @($invocation.Arguments)
    Assert-True (($args -join ' ') -match '-ReleaseBase https://github.com/vparla/GoalRouter/releases/download/v1.2.3') 'canonical release URL advances with version'
    Assert-True (($args -join ' ') -match '-Image registry.example/custom/router:1.2.3') 'custom image repository is preserved'
    Assert-True ($args -contains '-NoPathUpdate') 'no-PATH policy is preserved'
    $latestInvocation = New-GoalRouterMaintenanceInvocation -Command update -CommandArguments @() -Manifest $manifest -PhysicalLauncherPath $layout.LauncherPath -AuthMode existing-session
    $latestArgs = @($latestInvocation.Arguments)
    Assert-True (($latestArgs -join ' ') -match '-Version latest') 'bare update selects latest'
    Assert-True (($latestArgs -join ' ') -match '-Image registry.example/custom/router:latest') 'bare update selects latest image tag before resolution'
    Assert-True ($latestArgs -notcontains '-ReleaseBase') 'bare canonical update lets installer resolve latest release URL'
}

Invoke-Contract 'API-key update preserves explicit auth through post-switch doctor and rollback' {
    $fixture = New-FullInstallerFixture
    $options = New-FullInstallOptions
    Invoke-GoalRouterWindowsInstall -Options $options -Ports $fixture.Ports
    $oldControl = $fixture.State.Files['D:\Install\install.json']
    $fixture.State.Version = '1.0.1'; $fixture.State.Revision = 'api-key-update'; $fixture.State.ManifestJson = New-ReleaseManifestJson -Version '1.0.1' -Image 'ghcr.io/vparla/goalrouter:1.0.1' -Revision $fixture.State.Revision
    $options.Version = '1.0.1'; $options.ReleaseBase = 'https://github.com/vparla/GoalRouter/releases/download/v1.0.1'; $options.Image = 'ghcr.io/vparla/goalrouter:1.0.1'
    $options.AuthMode = 'api-key'
    $priorKey = $env:OPENAI_API_KEY
    try {
        $env:OPENAI_API_KEY = 'sk-secret-must-not-leak'
        $fixture.State.DoctorExitCode = 41
        Assert-Throws { Invoke-GoalRouterWindowsInstall -Options $options -Ports $fixture.Ports } 'doctor failed' 'API-key post-switch doctor rollback'
        Assert-Equal $fixture.State.Files['D:\Install\install.json'] $oldControl 'API-key update rollback restores prior control'
        $doctorCall = @($fixture.State.Calls | Where-Object { $_ -isnot [string] -and $_.Kind -ceq 'doctor' })[-1]
        Assert-True ((@($doctorCall.Arguments) -join ' ') -match '--auth-mode api-key') 'post-switch doctor retains API-key mode'
        Assert-True ((@($doctorCall.Arguments) -join ' ') -notmatch 'sk-secret-must-not-leak') 'post-switch doctor argv excludes secret value'
        $fixture.State.DoctorExitCode = 0
        Invoke-GoalRouterWindowsInstall -Options $options -Ports $fixture.Ports
        Assert-Equal (($fixture.State.Files['D:\Install\install.json'] | ConvertFrom-Json).version) '1.0.1' 'API-key update roundtrip succeeds'
    } finally {
        if ($null -eq $priorKey) { Remove-Item Env:OPENAI_API_KEY -ErrorAction Stop } else { $env:OPENAI_API_KEY = $priorKey }
    }
}

Invoke-Contract 'install rollback attempts every restoration and restores PATH after setter failure' {
    $events = [System.Collections.ArrayList]::new()
    $paths = [ordered]@{ 'D:\a' = 'new-a'; 'D:\b' = 'new-b' }
    $ports = [pscustomobject]@{
        Snapshot = { param([string]$Path); [pscustomobject]@{ Present = $true; Content = "old-$Path" } }
        Replace = { param([string]$Path, [string]$Content); [void]$events.Add("replace:$Path") }.GetNewClosure()
        Restore = { param([string]$Path, $Snapshot); [void]$events.Add("restore:$Path"); if ($Path -ceq 'D:\b') { throw 'restore b failed' } }.GetNewClosure()
        GetUserPath = { [pscustomobject]@{ Present = $true; Value = 'before' } }
        SetUserPath = { param($Snapshot); [void]$events.Add("path:$($Snapshot.Value)"); if ($Snapshot.Value -ceq 'after') { throw 'broadcast failed' } }.GetNewClosure()
        Doctor = { return 0 }
    }
    $plan = [pscustomobject]@{ Replacements = $paths; PathChange = [pscustomobject]@{ Changed = $true; Snapshot = [pscustomobject]@{ Present = $true; Value = 'after' } }; Doctor = [pscustomobject]@{ FilePath = 'x'; Arguments = @() }; SkipDoctor = $true }
    Assert-Throws { Invoke-GoalRouterInstallCommit -Plan $plan -Ports $ports } 'broadcast failed|rollback' 'setter failure rolls back'
    Assert-True ($events -contains 'path:before') 'PATH restoration attempted after setter failure'
    Assert-True ($events -contains 'restore:D:\a') 'rollback continues after another restore fails'
}

Invoke-Contract 'candidate inspection binds platform and revision to the resolved RepoDigest' {
    $calls = [System.Collections.ArrayList]::new(); $digest = 'sha256:' + ('a' * 64); $repoDigest = "registry.example/router@$digest"; $digestState = [pscustomobject]@{ Output = @($repoDigest) }
    $native = {
        param([string]$FilePath, [string[]]$Arguments, [bool]$CaptureOutput)
        [void]$calls.Add(@($Arguments))
        $joined = $Arguments -join ' '
        if ($joined -match 'docker pull') { return [pscustomobject]@{ ExitCode = 0; Output = @() } }
        if ($joined -match 'RepoDigests') { return [pscustomobject]@{ ExitCode = 0; Output = @($digestState.Output) } }
        if ($joined -match '\.Architecture') { return [pscustomobject]@{ ExitCode = 0; Output = @('amd64') } }
        if ($joined -match 'image\.revision') { return [pscustomobject]@{ ExitCode = 0; Output = @('rev') } }
        return [pscustomobject]@{ ExitCode = 0; Output = @('{"version":"1.0.0","protocol_version":1}') }
    }.GetNewClosure()
    $manifest = (New-ReleaseManifestJson -Image 'registry.example/router:1.0.0' -Revision 'rev') | ConvertFrom-Json
    [void](Test-GoalRouterCandidateImage -Manifest $manifest -Distribution Ubuntu -Platform 'linux/amd64' -NativeInvoker $native)
    $inspectCalls = @($calls | Where-Object { ($_ -join ' ') -match '\.Architecture|image\.revision' })
    foreach ($call in $inspectCalls) { Assert-Equal $call[-1] $repoDigest 'immutable inspect target' }
    foreach ($extra in @('other.example/router@sha256:' + ('b' * 64), 'unexpected diagnostic')) {
        $calls.Clear(); $digestState.Output = @($repoDigest, $extra)
        Assert-Throws { Test-GoalRouterCandidateImage -Manifest $manifest -Distribution Ubuntu -Platform 'linux/amd64' -NativeInvoker $native } 'one canonical repository digest' "extra raw RepoDigest output $extra"
        Assert-True ((@($calls | ForEach-Object { $_ -join ' ' }) -join "`n") -notmatch 'docker run') 'extra raw RepoDigest output fails before candidate execution'
    }
}

Invoke-Contract 'host version guards require PowerShell 5.1 and query selected WSL version' {
    Assert-GoalRouterPowerShellVersion -Version '5.1.19041.1'
    Assert-Throws { Assert-GoalRouterPowerShellVersion -Version '5.0.0' } '5\.1' 'PowerShell 5.0 rejected'
    $calls = [System.Collections.ArrayList]::new()
    $native = { param([string]$FilePath, [string[]]$Arguments, [bool]$CaptureOutput); [void]$calls.Add(($Arguments -join ' ')); [pscustomobject]@{ ExitCode = 0; Output = @('WSL version: 2.3.24.0') } }.GetNewClosure()
    Assert-Equal (Get-GoalRouterWslVersion -Distribution 'Ubuntu-Trusted' -NativeInvoker $native) '2.3.24.0' 'actual WSL version'
    Assert-True ((@($calls) -join ' ') -match 'wslinfo.*--wsl-version') 'selected distribution is queried'
}

Invoke-Contract 'WSL Docker version and architecture probes require exact single-line output' {
    $cleanWsl = { param([string]$FilePath, [string[]]$Arguments, [bool]$CaptureOutput); return [pscustomobject]@{ ExitCode = 0; Output = @('WSL version: 2.3.24') } }
    Assert-Equal (Get-GoalRouterWslVersion -Distribution Ubuntu -NativeInvoker $cleanWsl) '2.3.24' 'canonical WSL output'
    $decoratedWsl = { param([string]$FilePath, [string[]]$Arguments, [bool]$CaptureOutput); return [pscustomobject]@{ ExitCode = 0; Output = @('prefix 2.3.24 suffix') } }
    Assert-Throws { Get-GoalRouterWslVersion -Distribution Ubuntu -NativeInvoker $decoratedWsl } 'output is invalid' 'decorated WSL output'
    Assert-Equal (ConvertFrom-GoalRouterDockerVersionOutput -Output @('28.3.3 28.3.3')) @('28.3.3', '28.3.3') 'canonical Docker versions'
    foreach ($invalid in @(@('prefix 28.3.3 28.3.3'), @('28.3.3 28.3.3', 'extra'), @('28.3.3  28.3.3'))) {
        $candidate = $invalid
        Assert-Throws { ConvertFrom-GoalRouterDockerVersionOutput -Output @($candidate) } 'output is invalid' 'noncanonical Docker version output'
    }
    Assert-Equal (ConvertFrom-GoalRouterDockerArchitectureOutput -Output @('x86_64')) 'x86_64' 'canonical Docker architecture'
    Assert-Throws { ConvertFrom-GoalRouterDockerArchitectureOutput -Output @('x86_64 extra') } 'output is invalid' 'decorated Docker architecture'
    Assert-Throws { ConvertFrom-GoalRouterDockerArchitectureOutput -Output @('x86_64', 'extra') } 'output is invalid' 'multiline Docker architecture'
}

Invoke-Contract 'Windows PATH equivalence canonicalizes trailing separators without rewriting bytes' {
    Assert-True (Test-GoalRouterWindowsPathEquivalent -First 'C:\GoalRouter\bin\' -Second 'c:\goalrouter\BIN') 'trailing separator equivalent'
    Assert-True (Test-GoalRouterWindowsPathEquivalent -First 'C:/GoalRouter/bin/' -Second 'c:\goalrouter\BIN') 'mixed separators equivalent'
    $before = [pscustomobject]@{ Present = $true; Value = 'C:\Tools;C:\GoalRouter\bin\' }
    $result = Add-GoalRouterUserPathEntry -Snapshot $before -OwnedEntry 'c:\goalrouter\bin'
    Assert-True (-not $result.Changed) 'canonical duplicate is not added'
    Assert-Equal $result.Snapshot.Value $before.Value 'original PATH bytes are retained'
}

Invoke-Contract 'public install rejects undiscoverable custom bin before any mutation' {
    $fixture = New-FullInstallerFixture
    $options = New-FullInstallOptions
    $options.BinDir = 'D:\Elsewhere\bin'
    Assert-Throws { Invoke-GoalRouterWindowsInstall -Options $options -Ports $fixture.Ports } 'install root bin child|discoverable' 'custom bin refusal'
    Assert-Equal $fixture.State.Mutations.Count 0 'custom bin rejection precedes mutation'
}

Invoke-Contract 'ownership ACL and recursive reparse evidence fail closed for adoption and purge' {
    $foreignOwner = New-SafePathInfo -Path 'D:\State'; $foreignOwner.OwnerMatchesCurrentUser = $false
    Assert-Throws { Assert-GoalRouterInstallDestination -Info $foreignOwner -Label 'state' -AllowedEntries @() } 'owned by the current user' 'foreign owner'
    $unsafeAcl = New-SafePathInfo -Path 'D:\State'; $unsafeAcl.AclIsSafe = $false
    Assert-Throws { Assert-GoalRouterInstallDestination -Info $unsafeAcl -Label 'state' -AllowedEntries @() } 'unsafe write' 'unsafe ACL'
    $nestedLink = New-SafePathInfo -Path 'D:\Config'; $nestedLink.ContainsReparsePoint = $true
    Assert-Throws { Assert-GoalRouterInstallDestination -Info $nestedLink -Label 'state' -AllowedEntries @() } 'recursive reparse' 'nested adoption reparse'
    $manifest = [ordered]@{ owned = [ordered]@{ install_root = 'D:\Install'; config_dir = 'D:\Config'; config_file = 'D:\Config\task-models.yaml'; state_dir = 'D:\State' }; path_ownership = [ordered]@{ installer_added = $false; update_enabled = $false; owned_value = 'D:\Install\bin'; before_state = 'absent'; before_value_kind = $null; after_value_kind = $null; after_sha256 = $null } }
    Assert-Throws { New-GoalRouterUninstallPlan -Manifest $manifest -Purge $true -PathInfos @{ Config = $nestedLink; State = New-SafePathInfo -Path 'D:\State' } -CurrentUserPath ([pscustomobject]@{ Present = $false; Value = $null }) -RecoveryMode $null } 'recursive reparse' 'nested purge reparse'
    $foreign = New-SafePathInfo -Path 'D:\Config' -Entries @($script:GoalRouterDirectorySentinel, 'foreign.txt')
    Assert-Throws { New-GoalRouterUninstallPlan -Manifest $manifest -Purge $true -PathInfos @{ Config = $foreign; State = New-SafePathInfo -Path 'D:\State' } -CurrentUserPath ([pscustomobject]@{ Present = $false; Value = $null }) -RecoveryMode $null } 'foreign content' 'foreign purge content'
    $dotPath = [pscustomobject]@{ Path = 'D:\Safe\..\Other'; ProviderName = 'FileSystem'; ProviderPath = 'D:\Safe\..\Other'; Exists = $false; IsContainer = $false; IsLeaf = $false; IsReparsePoint = $false; ParentIsReparsePoint = $false; Entries = @() }
    Assert-Throws { Assert-GoalRouterLifecyclePathInfo -Info $dotPath -Label 'dot path' -AllowMissing $true -ProtectedRoots @() } 'dot path segments' 'dot segment refusal'
    foreach ($ambiguous in @('D:\Safe\\Other', 'D:\Safe\file.txt:stream', 'D:\Safe\Other.', 'D:\Safe\Other ')) {
        $info = [pscustomobject]@{ Path = $ambiguous; ProviderName = 'FileSystem'; ProviderPath = $ambiguous; Exists = $false; IsContainer = $false; IsLeaf = $false; IsReparsePoint = $false; ParentIsReparsePoint = $false; Entries = @() }
        Assert-Throws { Assert-GoalRouterLifecyclePathInfo -Info $info -Label 'ambiguous path' -AllowMissing $true -ProtectedRoots @() } 'ambiguous' "ambiguous Windows path $ambiguous"
    }
    $unicodePath = [pscustomobject]@{ Path = 'D:\Users\José\GoalRouter'; ProviderName = 'FileSystem'; ProviderPath = 'D:\Users\José\GoalRouter'; Exists = $false; IsContainer = $false; IsLeaf = $false; IsReparsePoint = $false; ParentIsReparsePoint = $false; Entries = @() }
    [void](Assert-GoalRouterLifecyclePathInfo -Info $unicodePath -Label 'Unicode path' -AllowMissing $true -ProtectedRoots @())
}

Invoke-Contract 'uninstall root rejects nested reparse evidence before recovery mutation' {
    $source = [IO.File]::ReadAllText($uninstaller)
    $gate = $source.Substring($source.IndexOf('$installRootInfo ='), $source.IndexOf('$manifestPath =') - $source.IndexOf('$installRootInfo ='))
    Assert-True $gate.Contains('ContainsReparsePoint') 'install-root gate checks recursive reparse evidence'
}

Invoke-Contract 'public purge rejects an unsafe higher ancestor before first deletion' {
    $fixture = New-FullInstallerFixture
    $options = New-FullInstallOptions
    Invoke-GoalRouterWindowsInstall -Options $options -Ports $fixture.Ports
    $unsafeState = [pscustomobject]@{ Path = 'D:\State'; ProviderName = 'FileSystem'; ProviderPath = 'D:\State'; Exists = $true; IsContainer = $true; IsLeaf = $false; IsReparsePoint = $false; ParentIsReparsePoint = $false; OwnerMatchesCurrentUser = $true; AclIsSafe = $true; AncestorChainIsSafe = $false }
    $fixture.State.ResolveOverrides['D:\State'] = $unsafeState
    $fixture.State.Removals.Clear()
    Assert-Throws { Invoke-GoalRouterWindowsUninstall -SelectedInstallRoot 'D:\Install' -SelectedPurge $true -Confirmed $true -Ports $fixture.Ports -PhysicalUninstallerPath 'D:\Install\bin\uninstall.ps1' } 'ancestor chain' 'unsafe higher purge ancestor'
    Assert-Equal $fixture.State.Removals.Count 0 'unsafe higher purge ancestor fails before deletion'
}

Invoke-Contract 'public uninstall resumes from checksummed journal after trusted control deletion' {
    $fixture = New-FullInstallerFixture
    Invoke-GoalRouterWindowsInstall -Options (New-FullInstallOptions) -Ports $fixture.Ports
    $manifest = $fixture.State.Files['D:\Install\install.json'] | ConvertFrom-Json
    $recovery = New-GoalRouterUninstallRecoveryRecord -Mode 'preserve' -Phase 'final' -Manifest $manifest
    $recoveryJson = ConvertTo-GoalRouterCanonicalJson $recovery
    $fixture.State.Files['D:\Install\uninstall-recovery.json'] = $recoveryJson
    [void]$fixture.State.Files.Remove('D:\Install\install.json')
    [void]$fixture.State.Files.Remove('D:\Install\install.sha256')
    Invoke-GoalRouterWindowsUninstall -SelectedInstallRoot 'D:\Install' -SelectedPurge $false -Confirmed $false -Ports $fixture.Ports -PhysicalUninstallerPath 'D:\Install\bin\uninstall.ps1'
    Assert-True (-not $fixture.State.Files.ContainsKey('D:\Install\bin\uninstall.ps1')) 'journal retry removes uninstaller'
    Assert-True (-not $fixture.State.Files.ContainsKey('D:\Install\uninstall-recovery.json')) 'journal retry completes cleanup'
}

Invoke-Contract 'public update round preserves PATH and rolls back every installed surface before succeeding' {
    $fixture = New-FullInstallerFixture
    $options = New-FullInstallOptions
    Invoke-GoalRouterWindowsInstall -Options $options -Ports $fixture.Ports
    $oldControl = $fixture.State.Files['D:\Install\install.json']
    $oldLauncher = $fixture.State.Files['D:\Install\bin\goalrouter.ps1']
    $oldPath = $fixture.State.UserPath.Value
    $fixture.State.Version = '1.0.1'
    $fixture.State.Revision = 'revision-1.0.1'
    $fixture.State.ManifestJson = New-ReleaseManifestJson -Version '1.0.1' -Image 'ghcr.io/vparla/goalrouter:1.0.1' -Revision $fixture.State.Revision
    $options.Version = '1.0.1'
    $options.ReleaseBase = 'https://github.com/vparla/GoalRouter/releases/download/v1.0.1'
    $options.Image = 'ghcr.io/vparla/goalrouter:1.0.1'
    $fixture.State.DoctorExitCode = 23
    Assert-Throws { Invoke-GoalRouterWindowsInstall -Options $options -Ports $fixture.Ports } 'doctor failed' 'failed update doctor'
    Assert-Equal $fixture.State.Files['D:\Install\install.json'] $oldControl 'failed update restores trusted control'
    Assert-Equal $fixture.State.Files['D:\Install\bin\goalrouter.ps1'] $oldLauncher 'failed update restores launcher'
    Assert-Equal $fixture.State.UserPath.Value $oldPath 'failed update restores PATH bytes'
    $fixture.State.DoctorExitCode = 0
    Invoke-GoalRouterWindowsInstall -Options $options -Ports $fixture.Ports
    $updated = $fixture.State.Files['D:\Install\install.json'] | ConvertFrom-Json
    Assert-Equal $updated.version '1.0.1' 'successful update version'
    Assert-Equal $updated.source_revision 'revision-1.0.1' 'successful update revision'
    Assert-Equal $fixture.State.UserPath.Value $oldPath 'successful update does not duplicate PATH'
}

Invoke-Contract 'public latest update resolves stable version before release and image selection' {
    $fixture = New-FullInstallerFixture
    $options = New-FullInstallOptions
    Invoke-GoalRouterWindowsInstall -Options $options -Ports $fixture.Ports
    $fixture.State.Version = '1.0.2'; $fixture.State.Revision = 'latest-resolution'; $fixture.State.ManifestJson = New-ReleaseManifestJson -Version '1.0.2' -Image 'ghcr.io/vparla/goalrouter:1.0.2' -Revision $fixture.State.Revision
    $options.Version = 'latest'; $options.ReleaseBase = $null; $options.Image = 'ghcr.io/vparla/goalrouter:latest'
    $fixture.State.Calls.Clear()
    Invoke-GoalRouterWindowsInstall -Options $options -Ports $fixture.Ports
    $installed = $fixture.State.Files['D:\Install\install.json'] | ConvertFrom-Json
    Assert-Equal $installed.version '1.0.2' 'latest resolves to stable semantic version'
    Assert-True ((@($fixture.State.Calls | Where-Object { $_ -is [string] }) -join "`n") -match 'download:https://github.com/vparla/GoalRouter/releases/download/v1.0.2/SHA256SUMS') 'latest uses resolved canonical release URL'
    Assert-Equal $installed.image_reference 'ghcr.io/vparla/goalrouter' 'latest stores repository-only image authority'
}

Invoke-Contract 'failed clean install removes only directories created by that transaction' {
    $fixture = New-FullInstallerFixture
    $fixture.State.DoctorExitCode = 31
    Assert-Throws { Invoke-GoalRouterWindowsInstall -Options (New-FullInstallOptions) -Ports $fixture.Ports } 'doctor failed' 'clean install doctor failure'
    foreach ($path in @('D:\Install\bin', 'D:\Install', 'D:\Config', 'D:\State')) { Assert-True (-not $fixture.State.Directories.Contains($path)) "created directory rolled back: $path" }
    Assert-True (-not $fixture.State.Directories.Contains('D:\Codex')) 'preexisting Codex directory model is not adopted'
}

Invoke-Contract 'failed install removes every created intermediate directory deepest first' {
    $events = [System.Collections.ArrayList]::new()
    $ports = [pscustomobject]@{
        Snapshot = { param([string]$Path); [pscustomobject]@{ Present = $false; Content = $null } }
        Replace = { throw 'injected replacement failure' }
        Restore = { param([string]$Path, $Snapshot) }
        GetUserPath = { [pscustomobject]@{ Present = $false; Value = $null } }
        SetUserPath = { param($Snapshot) }
        Doctor = { return 0 }
        EnsureDirectory = { param([string]$Path); return @('D:\new', 'D:\new\parent', $Path) }
        RemoveTree = { param([string]$Path); [void]$events.Add($Path) }.GetNewClosure()
    }
    $plan = [pscustomobject]@{ Directories = @('D:\new\parent\GoalRouter'); Replacements = [ordered]@{ 'D:\new\parent\GoalRouter\file' = 'new' }; PathChange = [pscustomobject]@{ Changed = $false; Snapshot = [pscustomobject]@{ Present = $false; Value = $null } }; Doctor = [pscustomobject]@{ FilePath = 'unused'; Arguments = @() }; SkipDoctor = $true }
    Assert-Throws { Invoke-GoalRouterInstallCommit -Plan $plan -Ports $ports } 'injected replacement failure' 'nested directory rollback'
    Assert-Equal $events @('D:\new\parent\GoalRouter', 'D:\new\parent', 'D:\new') 'intermediate rollback order'
}

Invoke-Contract 'directory-chain creation rolls back its own partial failure' {
    $directories = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    [void]$directories.Add('/fixture')
    $removed = [System.Collections.ArrayList]::new()
    $testDirectory = { param([string]$Path); return $directories.Contains($Path) }.GetNewClosure()
    $createDirectory = { param([string]$Path); if ($Path -ceq '/fixture/new/parent') { throw 'injected mid-chain failure' }; [void]$directories.Add($Path) }.GetNewClosure()
    $getAttributes = { return [IO.FileAttributes]::Directory }
    $removeDirectory = { param([string]$Path); [void]$removed.Add($Path); [void]$directories.Remove($Path) }.GetNewClosure()
    Assert-Throws { Ensure-GoalRouterDirectoryChain -Path '/fixture/new/parent/GoalRouter' -TestDirectory $testDirectory -CreateDirectory $createDirectory -GetAttributes $getAttributes -RemoveDirectory $removeDirectory } 'mid-chain failure' 'partial directory-chain failure'
    Assert-Equal $removed @('/fixture/new') 'partial chain rollback removes created ancestor'
}

Invoke-Contract 'temporary staging root and created workdir are trusted before download use' {
    $events = [System.Collections.ArrayList]::new()
    $safeInfo = { param([string]$Path); [pscustomobject]@{ Path = $Path; ProviderName = 'FileSystem'; ProviderPath = $Path; Exists = $true; IsContainer = $true; IsLeaf = $false; IsReparsePoint = $false; ParentIsReparsePoint = $false; OwnerMatchesCurrentUser = $true; OwnerIsTrusted = $true; AclIsSafe = $true; AncestorChainIsSafe = $true } }
    $resolve = { param([string]$Path, [string]$Kind, [bool]$AllowMissing); & $safeInfo $Path }.GetNewClosure()
    $created = New-GoalRouterTrustedWorkDirectory -TempRoot 'D:\SafeTemp' -ResolvePathPort $resolve -CreateDirectoryPort { param([string]$Path); [void]$events.Add("create:$Path") } -RemoveDirectoryPort { param([string]$Path); [void]$events.Add("remove:$Path") } -NewNamePort { 'goalrouter-install-fixed' }
    Assert-Equal $created 'D:\SafeTemp\goalrouter-install-fixed' 'trusted staging path'
    $foreignOwner = { param([string]$Path); [pscustomobject]@{ Path = $Path; ProviderName = 'FileSystem'; ProviderPath = $Path; Exists = $true; IsContainer = $true; IsLeaf = $false; IsReparsePoint = $false; ParentIsReparsePoint = $false; OwnerMatchesCurrentUser = $false; OwnerIsTrusted = $true; AclIsSafe = $true; AncestorChainIsSafe = $true } }
    Assert-Throws { New-GoalRouterTrustedWorkDirectory -TempRoot 'D:\ForeignTemp' -ResolvePathPort $foreignOwner -CreateDirectoryPort { throw 'must not create' } -RemoveDirectoryPort { param([string]$Path) } -NewNamePort { 'goalrouter-install-fixed' } } 'owned by the current user' 'trusted non-user staging root is rejected'
    $unsafeResolve = { param([string]$Path, [string]$Kind, [bool]$AllowMissing); $info = & $safeInfo $Path; $info.ProviderPath = 'D:\Redirected'; $info.IsReparsePoint = $true; return $info }.GetNewClosure()
    Assert-Throws { New-GoalRouterTrustedWorkDirectory -TempRoot 'D:\UnsafeTemp' -ResolvePathPort $unsafeResolve -CreateDirectoryPort { throw 'must not create' } -RemoveDirectoryPort { param([string]$Path) } -NewNamePort { 'goalrouter-install-fixed' } } 'provider|reparse' 'redirected staging root'
    Assert-Equal @($events | Where-Object { $_ -like 'create:*' }).Count 1 'unsafe staging root creates nothing'
}

Invoke-Contract 'recovery journal is one atomic checksummed record and self-removal is the last fallible action' {
    $manifest = [ordered]@{ owned = [ordered]@{ install_root = 'D:\Install' } }
    $record = New-GoalRouterUninstallRecoveryRecord -Mode preserve -Phase start -Manifest $manifest
    Assert-True ($record.manifest_sha256 -cmatch '\A[0-9a-f]{64}\z') 'journal embeds manifest checksum'
    Assert-Equal $record.manifest_sha256 (Get-GoalRouterStringSha256 (ConvertTo-GoalRouterCanonicalJson $manifest)) 'embedded checksum binds manifest'
    $source = [IO.File]::ReadAllText($uninstaller)
    Assert-True (-not $source.Contains('RecoveryChecksumPath')) 'journal has no independently replaceable checksum pair'
    $commit = $source.Substring($source.IndexOf('function Invoke-GoalRouterUninstallCommit'), $source.IndexOf('function Invoke-GoalRouterWindowsUninstall') - $source.IndexOf('function Invoke-GoalRouterUninstallCommit'))
    $cleanupJournal = $commit.LastIndexOf("-Phase 'cleanup'")
    $recoveryRemoval = $commit.LastIndexOf('Plan.RecoveryPath')
    $installerRemoval = $commit.LastIndexOf('Plan.InstallerPath')
    $selfRemoval = $commit.LastIndexOf('UninstallerPath')
    Assert-True ($cleanupJournal -ge 0 -and $selfRemoval -gt $cleanupJournal) 'cleanup journal is durable before self-removal'
    Assert-True ($recoveryRemoval -gt $cleanupJournal -and $installerRemoval -gt $recoveryRemoval -and $selfRemoval -gt $installerRemoval) 'recovery is removed before the retained helper and physical uninstaller'
    Assert-True ($commit.Substring($selfRemoval) -notmatch 'writeJournal|replacePort') 'no fallible journal write follows self-removal'
    $installerSource = [IO.File]::ReadAllText($installer)
    $replaceStart = $installerSource.IndexOf('$replace = {')
    $replaceBlock = $installerSource.Substring($replaceStart, $installerSource.IndexOf('$restore = {') - $replaceStart)
    Assert-True $replaceBlock.Contains('[IO.File]::Replace($temporary, $Path, $null, $true)') 'existing journal targets use same-volume atomic replacement without orphan backup'
    Assert-True (-not $replaceBlock.Contains('Move-Item -LiteralPath $Path -Destination $backup')) 'existing target is never moved away before replacement'
    Assert-True (-not $replaceBlock.Contains("'.bak'")) 'atomic replacement creates no untracked backup residue'
}

Invoke-Contract 'public bounded final cleanup retries only canonical lifecycle residuals' {
    $fixture = New-FullInstallerFixture
    [void]$fixture.State.Directories.Add('D:\Install')
    [void]$fixture.State.Directories.Add('D:\Install\bin')
    $fixture.State.Files['D:\Install\bin\install.ps1'] = 'residual-library'
    $fixture.State.Files['D:\Install\bin\uninstall.ps1'] = 'residual-uninstaller'
    Invoke-GoalRouterWindowsUninstall -SelectedInstallRoot 'D:\Install' -SelectedPurge $false -Confirmed $false -Ports $fixture.Ports -PhysicalUninstallerPath 'D:\Install\bin\uninstall.ps1'
    Assert-True (-not $fixture.State.Files.ContainsKey('D:\Install\bin\install.ps1')) 'residual helper cleanup completed'
    Assert-True (-not $fixture.State.Files.ContainsKey('D:\Install\bin\uninstall.ps1')) 'residual self cleanup completed'
    $fixture.State.Files['D:\Install\bin\uninstall.ps1'] = 'residual-uninstaller'
    Assert-Throws { Invoke-GoalRouterWindowsUninstall -SelectedInstallRoot 'D:\Install' -SelectedPurge $false -Confirmed $false -Ports $fixture.Ports -PhysicalUninstallerPath 'D:\Elsewhere\uninstall.ps1' } 'trusted install control is missing' 'self cleanup cannot redirect'
    $fixture.State.Files['D:\Install\install.sha256'] = ('a' * 64) + "`n"
    Assert-Throws { Invoke-GoalRouterWindowsUninstall -SelectedInstallRoot 'D:\Install' -SelectedPurge $false -Confirmed $false -Ports $fixture.Ports -PhysicalUninstallerPath 'D:\Install\bin\uninstall.ps1' } 'incomplete' 'self cleanup refuses residual checksum authority'
}

Invoke-Contract 'pre-import lifecycle and bounded self cleanup require typed trusted physical leaves' {
    $safe = [pscustomobject]@{ Count = 1; ProviderName = 'FileSystem'; ProviderPath = 'D:\Install\bin\uninstall.ps1'; IsLeaf = $true; IsReparsePoint = $false; OwnerMatchesCurrentUser = $true; AclIsSafe = $true; AncestorChainIsSafe = $true }
    Assert-Equal (Assert-GoalRouterBootstrapLeaf -Path 'D:\Install\bin\uninstall.ps1' -ExpectedPath 'D:\Install\bin\uninstall.ps1' -Inspection $safe) 'D:\Install\bin\uninstall.ps1' 'safe bootstrap leaf'
    $caseVaried = $safe.PSObject.Copy(); $caseVaried.ProviderPath = 'd:/INSTALL/bin/uninstall.ps1'
    Assert-Equal (Assert-GoalRouterBootstrapLeaf -Path 'd:/install/bin/uninstall.ps1' -ExpectedPath 'D:\Install\bin\uninstall.ps1' -Inspection $caseVaried) 'd:/INSTALL/bin/uninstall.ps1' 'case- and separator-varied identical bootstrap leaf'
    foreach ($change in @(
        @{ Name = 'ProviderPath'; Value = 'D:\Redirected\uninstall.ps1'; Pattern = 'provider|exact' },
        @{ Name = 'IsLeaf'; Value = $false; Pattern = 'leaf' },
        @{ Name = 'IsReparsePoint'; Value = $true; Pattern = 'reparse' },
        @{ Name = 'OwnerMatchesCurrentUser'; Value = $false; Pattern = 'owned|owner' },
        @{ Name = 'AclIsSafe'; Value = $false; Pattern = 'ACL' },
        @{ Name = 'AncestorChainIsSafe'; Value = $false; Pattern = 'ancestor' }
    )) {
        $inspection = $safe.PSObject.Copy(); $inspection.($change.Name) = $change.Value
        Assert-Throws { Assert-GoalRouterBootstrapLeaf -Path 'D:\Install\bin\uninstall.ps1' -ExpectedPath 'D:\Install\bin\uninstall.ps1' -Inspection $inspection } $change.Pattern "bootstrap $($change.Name)"
    }
    $source = [IO.File]::ReadAllText($uninstaller)
    $physicalProofStart = $source.IndexOf('function Assert-GoalRouterBootstrapPhysicalLeaf')
    $physicalProof = $source.Substring($physicalProofStart, $source.IndexOf('$selectedInstallRootArgument') - $physicalProofStart)
    foreach ($ancestorProof in @('$cursor = Split-Path -Parent', '[IO.File]::GetAttributes($cursor)', 'Get-Acl -LiteralPath $cursor -ErrorAction Stop', '$ancestorOwnerSid -notin $allowedSids', "-cmatch '\A[A-Za-z]:\z'")) {
        Assert-True $physicalProof.Contains($ancestorProof) "bootstrap physical proof contains $ancestorProof"
    }
    $importIndex = $source.IndexOf('. $resolvedInstallerLibrary')
    $libraryProofIndex = $source.IndexOf('Assert-GoalRouterBootstrapPhysicalLeaf -Path $installerLibrary')
    Assert-True ($libraryProofIndex -ge 0 -and $libraryProofIndex -lt $importIndex) 'installer sibling is physically proven before dot-source import'
    $fallback = $source.Substring($source.IndexOf("if (-not (Test-Path -LiteralPath `$installerLibrary -PathType Leaf))"), $importIndex - $source.IndexOf("if (-not (Test-Path -LiteralPath `$installerLibrary -PathType Leaf))"))
    Assert-True $fallback.Contains('Assert-GoalRouterBootstrapPhysicalLeaf -Path $PSCommandPath') 'bounded self target is physically proven'
    Assert-True $fallback.Contains('Remove-Item -LiteralPath $resolvedSelfTarget -ErrorAction Stop') 'bounded self target uses typed literal deletion'
    Assert-True (-not $fallback.Contains('[IO.File]::Delete')) 'bounded fallback has no untyped file deletion'
    Assert-True ($source.IndexOf('try {', $source.IndexOf('$selectedInstallRootArgument')) -lt $source.IndexOf("if (-not (Test-Path -LiteralPath `$installerLibrary")) 'pre-import bootstrap is inside terminating boundary'
    Assert-True $source.Contains("[Console]::Error.WriteLine('goalrouter uninstaller: bootstrap trust validation failed')") 'bootstrap boundary emits a stable secret-free category'
}

Invoke-Contract 'missing destination requires safe nearest existing parent ownership and ACL' {
    $missing = [pscustomobject]@{ Path = 'D:\Parent\Child'; ProviderName = 'FileSystem'; ProviderPath = 'D:\Parent\Child'; Exists = $false; IsContainer = $false; IsLeaf = $false; IsReparsePoint = $false; ParentIsReparsePoint = $false; AncestorOwnerMatchesCurrentUser = $false; AncestorAclIsSafe = $true; Entries = @() }
    Assert-Throws { Assert-GoalRouterLifecyclePathInfo -Info $missing -Label 'child' -AllowMissing $true -ProtectedRoots @() } 'ancestor.*owned|owned.*ancestor' 'foreign parent owner'
    $missing.AncestorOwnerMatchesCurrentUser = $true; $missing.AncestorAclIsSafe = $false
    Assert-Throws { Assert-GoalRouterLifecyclePathInfo -Info $missing -Label 'child' -AllowMissing $true -ProtectedRoots @() } 'ancestor.*ACL|unsafe.*ancestor' 'unsafe inherited ACL'
}

Invoke-Contract 'existing custom config file requires safe ownership and ACL' {
    $unsafeOwner = [pscustomobject]@{ Path = 'D:\Config\task-models.yaml'; ProviderName = 'FileSystem'; ProviderPath = 'D:\Config\task-models.yaml'; Exists = $true; IsContainer = $false; IsLeaf = $true; IsReparsePoint = $false; ParentIsReparsePoint = $false; OwnerMatchesCurrentUser = $false; AclIsSafe = $true }
    Assert-Throws { Assert-GoalRouterLifecyclePathInfo -Info $unsafeOwner -Label 'config file' -AllowMissing $true -ProtectedRoots @() -RequiredKind File } 'owned|owner' 'foreign config owner'
    $unsafeOwner.OwnerMatchesCurrentUser = $true; $unsafeOwner.AclIsSafe = $false
    Assert-Throws { Assert-GoalRouterLifecyclePathInfo -Info $unsafeOwner -Label 'config file' -AllowMissing $true -ProtectedRoots @() -RequiredKind File } 'ACL|unsafe' 'unsafe config ACL'
    $source = [IO.File]::ReadAllText($installer)
    Assert-True (-not $source.Contains("`$script:GoalRouterRecoveryName + '.sha256'")) 'legacy recovery checksum is not allowlisted'
    $resolveBlock = $source.Substring($source.IndexOf('$resolvePath = {'), $source.IndexOf('$newWorkDirectory = {') - $source.IndexOf('$resolvePath = {'))
    Assert-True ($resolveBlock.Contains('Get-GoalRouterWindowsPathSecurity') -and $resolveBlock.Contains('OwnerMatchesCurrentUser = [bool]$pathSecurity.OwnerMatchesCurrentUser')) 'production existing file exposes real owner and ACL evidence'
    Assert-True ($resolveBlock.Contains('path resolves through a redirected FileSystem provider') -and $resolveBlock.Contains('$ancestorProviderPath')) 'missing targets validate nearest provider-native ancestor identity'
}

Invoke-Contract 'SkipAccount still validates existing-session auth source and skips only inventory' {
    $calls = [System.Collections.ArrayList]::new()
    $script:GoalRouterNativeInvoker = { param([string]$FilePath, [string[]]$Arguments, [bool]$CaptureOutput); [void]$calls.Add(($Arguments -join ' ')); [pscustomobject]@{ ExitCode = 0; Output = @() } }.GetNewClosure()
    $authRoot = Join-Path ([IO.Path]::GetTempPath()) ('goalrouter-auth-' + [guid]::NewGuid().ToString('N'))
    [void][IO.Directory]::CreateDirectory($authRoot)
    try {
        $context = [pscustomobject]@{ Distribution = 'Ubuntu'; Image = 'registry/router@sha256:' + ('a' * 64); Config = '/config'; State = '/state'; Project = '/project'; CodexHome = '/codex'; CodexWindows = $authRoot; AuthMode = 'existing-session'; Access = 'readonly'; Json = $false; Forwarded = @() }
        Assert-Throws { Invoke-GoalRouterInstalledDoctor -Context $context -SkipAccount $true } 'auth|session' 'missing auth source'
        [IO.File]::WriteAllText((Join-Path $authRoot 'auth.json'), '{}', [Text.UTF8Encoding]::new($false))
        Assert-Equal (Invoke-GoalRouterInstalledDoctor -Context $context -SkipAccount $true) 0 'valid auth source'
        Assert-True ((@($calls) -join "`n") -notmatch '(?:^| )models(?: |$)') 'SkipAccount omits only inventory'
    } finally { Remove-Item -LiteralPath $authRoot -Recurse -ErrorAction Stop }
}

Invoke-Contract 'production rollback snapshot preserves bytes attributes and Windows security descriptor' {
    $source = [IO.File]::ReadAllText($installer)
    foreach ($required in @('ReadAllBytes', 'WriteAllBytes', 'GetSecurityDescriptorSddlForm', 'SetSecurityDescriptorSddlForm', 'SetAttributes')) { Assert-True $source.Contains($required) "rollback metadata primitive $required" }
    Assert-True ($source -match 'Content\s*=\s*\$content') 'production snapshot exposes decoded content to trusted control consumers'
}

Invoke-Contract 'production User PATH uses HKCU registry snapshots with exact value-kind fidelity' {
    $source = [IO.File]::ReadAllText($installer)
    Assert-True $source.Contains("[Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment'") 'User PATH uses HKCU Environment registry'
    Assert-True $source.Contains('[Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames') 'ExpandString reads preserve exact bytes'
    Assert-True $source.Contains("`$key.SetValue('Path'") 'present-empty PATH is written as a registry value'
    Assert-True $source.Contains("`$key.DeleteValue('Path', `$false)") 'absent PATH deletes only the User value'
    Assert-True (-not $source.Contains("[Environment]::SetEnvironmentVariable('Path'")) 'PowerShell 5.1 empty PATH does not flow through deleting environment API'
    Assert-True (-not $source.Contains("'Machine'")) 'Machine PATH is never accessed'
}

Invoke-Contract 'production native port fails closed on stderr even when exit code is zero' {
    $ports = New-GoalRouterProductionLifecyclePorts
    Assert-Throws { & $ports.Native -FilePath '/bin/sh' -Arguments @('-c', 'printf warning >&2; exit 0') -CaptureOutput $true } 'stderr|successful exit' 'stderr with zero exit'
    $source = [IO.File]::ReadAllText($installer)
    $doctorBlock = $source.Substring($source.IndexOf('$doctor = {'), $source.IndexOf('$removeFile = {') - $source.IndexOf('$doctor = {'))
    Assert-True ($doctorBlock.Contains('Management.Automation.ErrorRecord') -and $doctorBlock.Contains("`$exitCode -eq 0")) 'installed doctor rejects stderr with zero exit'
    Assert-True $source.Contains('throw new TimeoutException') 'zero-result environment broadcast always fails closed'
}

Invoke-Contract 'declared PowerShell compatibility contract is explicitly 5.1 and excludes Core-only commands' {
    foreach ($path in @($installer, $uninstaller, $launcher)) {
        $source = [IO.File]::ReadAllText($path)
        Assert-True $source.Contains('#requires -Version 5.1') "5.1 runtime declaration in $path"
        foreach ($coreOnly in @('Get-Error', 'Join-String', 'ForEach-Object -Parallel', 'ConvertFrom-Json -AsHashtable', 'Invoke-WebRequest -SkipHttpErrorCheck')) { Assert-True (-not $source.Contains($coreOnly)) "Core-only API $coreOnly absent from $path" }
    }
}

Invoke-Contract 'launcher and uninstaller use one normalized Windows path identity' {
    $launcherSource = [IO.File]::ReadAllText($launcher)
    $uninstallerSource = [IO.File]::ReadAllText($uninstaller)
    Assert-True $launcherSource.Contains('function Test-GoalRouterWindowsPathEquivalent') 'launcher path identity helper'
    Assert-True ($launcherSource -notmatch '\$PhysicalLauncherPath\s+-ine') 'launcher avoids raw physical path comparison'
    Assert-True ($uninstallerSource -notmatch 'manifest\.owned\.(?:install_root|uninstaller)\s+-ine') 'uninstaller avoids raw trusted path comparison'
    Assert-True $uninstallerSource.Contains('$selectedInstallRootArgument') 'uninstaller preserves bound InstallRoot across library import'
    Assert-True $uninstallerSource.Contains('$selectedConfirmedArgument') 'uninstaller preserves bound Yes across library import'
}

if ($script:Failed -ne 0) {
    [Console]::Error.WriteLine("$($script:Failed) lifecycle contract(s) failed; $($script:Passed) passed")
    exit 1
}
if ($Error.Count -ne 0) {
    [Console]::Error.WriteLine("$($Error.Count) unexpected PowerShell error record(s) remained")
    exit 1
}
Write-Output "$($script:Passed) lifecycle contracts passed with zero warnings and error records."
exit 0
