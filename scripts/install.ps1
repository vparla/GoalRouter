# SPDX-License-Identifier: MIT
# File: scripts/install.ps1
# Purpose: Windows per-user installer and updater for GoalRouter
#requires -Version 5.1

[CmdletBinding()]
param(
    [ValidatePattern('^(?:latest|[0-9]+\.[0-9]+\.[0-9]+)$')][string]$Version = '1.0.0',
    [string]$InstallRoot,
    [string]$BinDir,
    [string]$ConfigFile,
    [string]$StateDir,
    [string]$CodexHome,
    [string]$WslDistribution = 'Ubuntu',
    [switch]$Yes,
    [switch]$Force,
    [switch]$ResetConfig,
    [switch]$NoPathUpdate,
    [switch]$SkipDoctor,
    [switch]$SkipAccount,
    [ValidateSet('existing-session', 'api-key')][string]$AuthMode = 'existing-session',
    [string]$ReleaseBase,
    [switch]$AllowLoopbackHttp,
    [string]$Image
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$WarningPreference = 'Stop'

$script:GoalRouterProtocolVersion = 1
$script:GoalRouterDirectorySentinel = '.goalrouter-owned-v1'
$script:GoalRouterDirectorySentinelValue = 'goalrouter-owned-directory-v1'
$script:GoalRouterReleaseManifestName = 'release-manifest.json'
$script:GoalRouterRecoveryName = 'uninstall-recovery.json'
$script:GoalRouterZipEntryLimit = 4194304
$script:GoalRouterZipTotalLimit = 12582912
$script:GoalRouterZipRatioLimit = 200

function Test-GoalRouterLifecycleSingleLine {
    param([AllowEmptyString()][string]$Value)
    return $null -ne $Value -and $Value -cmatch '\A[\x20-\x7e]+\z'
}

function Test-GoalRouterWslDistribution {
    param([AllowEmptyString()][string]$Value)
    return (Test-GoalRouterLifecycleSingleLine $Value) -and -not $Value.StartsWith('-')
}

function Test-GoalRouterLifecyclePathText {
    param([AllowEmptyString()][string]$Value)
    return -not [string]::IsNullOrEmpty($Value) -and $Value -cnotmatch '[\x00-\x1f\x7f]'
}

function Test-GoalRouterLifecycleDigest {
    param([string]$Digest)
    return (Test-GoalRouterLifecycleSingleLine $Digest) -and $Digest -cmatch '\Asha256:[0-9a-f]{64}\z'
}

function Join-GoalRouterWindowsPath {
    param([Parameter(Mandatory = $true)][string]$Parent, [Parameter(Mandatory = $true)][string]$Child)
    return $Parent.TrimEnd('\', '/') + '\' + $Child.TrimStart('\', '/')
}

function Get-GoalRouterWindowsLayout {
    param(
        [Parameter(Mandatory = $true)][string]$LocalAppData,
        [Parameter(Mandatory = $true)][string]$AppData,
        [Parameter(Mandatory = $true)][string]$UserProfile,
        [string]$InstallRoot,
        [string]$BinDir,
        [string]$ConfigFile,
        [string]$StateDir,
        [string]$CodexHome
    )
    if ([string]::IsNullOrWhiteSpace($LocalAppData)) { throw 'LOCALAPPDATA is required' }
    if ([string]::IsNullOrWhiteSpace($AppData)) { throw 'APPDATA is required' }
    if ([string]::IsNullOrWhiteSpace($UserProfile)) { throw 'USERPROFILE is required' }
    $selectedInstallRoot = if ([string]::IsNullOrEmpty($InstallRoot)) { Join-GoalRouterWindowsPath $LocalAppData 'GoalRouter' } else { $InstallRoot }
    $selectedBin = if ([string]::IsNullOrEmpty($BinDir)) { Join-GoalRouterWindowsPath $selectedInstallRoot 'bin' } else { $BinDir }
    $selectedConfig = if ([string]::IsNullOrEmpty($ConfigFile)) { Join-GoalRouterWindowsPath (Join-GoalRouterWindowsPath $AppData 'GoalRouter') 'task-models.yaml' } else { $ConfigFile }
    $selectedState = if ([string]::IsNullOrEmpty($StateDir)) { Join-GoalRouterWindowsPath $selectedInstallRoot 'state' } else { $StateDir }
    $selectedCodex = if ([string]::IsNullOrEmpty($CodexHome)) { Join-GoalRouterWindowsPath $UserProfile '.codex' } else { $CodexHome }
    $configParent = $selectedConfig.Substring(0, [Math]::Max($selectedConfig.LastIndexOf('\'), $selectedConfig.LastIndexOf('/')))
    $configLeaf = Split-Path -Leaf $selectedConfig
    $sentinelPath = Join-GoalRouterWindowsPath $configParent $script:GoalRouterDirectorySentinel
    if ((Test-GoalRouterWindowsPathEquivalent -First $selectedConfig -Second $sentinelPath) -or $configLeaf.TrimEnd(' ', '.') -ieq $script:GoalRouterDirectorySentinel) { throw 'config file cannot collide with the ownership sentinel' }
    return [pscustomobject][ordered]@{
        InstallRoot = $selectedInstallRoot
        BinDir = $selectedBin
        ConfigFile = $selectedConfig
        ConfigDir = $configParent
        StateDir = $selectedState
        CodexHome = $selectedCodex
        ManifestPath = Join-GoalRouterWindowsPath $selectedInstallRoot 'install.json'
        RecoveryPath = Join-GoalRouterWindowsPath $selectedInstallRoot $script:GoalRouterRecoveryName
        LauncherPath = Join-GoalRouterWindowsPath $selectedBin 'goalrouter.ps1'
        CmdPath = Join-GoalRouterWindowsPath $selectedBin 'goalrouter.cmd'
        InstallerPath = Join-GoalRouterWindowsPath $selectedBin 'install.ps1'
        UninstallerPath = Join-GoalRouterWindowsPath $selectedBin 'uninstall.ps1'
    }
}

function New-GoalRouterWslArguments {
    param(
        [Parameter(Mandatory = $true)][string]$Distribution,
        [string[]]$Arguments
    )
    if (-not (Test-GoalRouterWslDistribution $Distribution)) {
        throw 'invalid WSL distribution'
    }
    if ($Arguments.Count -eq 0) { throw 'native arguments are required' }
    foreach ($argument in $Arguments) {
        if ($null -eq $argument -or $argument.IndexOf([char]0) -ge 0) { throw 'native arguments contain an invalid value' }
    }
    return @('-d', $Distribution, '--') + @($Arguments)
}

function Assert-GoalRouterReleaseUri {
    param([Parameter(Mandatory = $true)][string]$Uri, [bool]$AllowLoopbackHttp, [bool]$AllowRedirectQuery = $false)
    if (-not (Test-GoalRouterLifecycleSingleLine $Uri)) { throw 'release URI contains a control character' }
    if ($Uri.Contains('\')) { throw 'release URI contains a backslash' }
    $parsed = $null
    if (-not [Uri]::TryCreate($Uri, [UriKind]::Absolute, [ref]$parsed)) { throw 'release URI is invalid' }
    if ([string]::IsNullOrEmpty($parsed.Host) -or [string]::IsNullOrEmpty($parsed.Authority)) { throw 'release URI authority is invalid' }
    if (-not [string]::IsNullOrEmpty($parsed.UserInfo)) { throw 'release URI userinfo is forbidden' }
    if (-not [string]::IsNullOrEmpty($parsed.Query) -and (-not $AllowRedirectQuery -or $parsed.Scheme -cne 'https')) { throw 'release URI query is forbidden' }
    if (-not [string]::IsNullOrEmpty($parsed.Fragment)) { throw 'release URI fragment is forbidden' }
    if ($parsed.Scheme -ceq 'https') { return $parsed }
    $loopbackNames = @('127.0.0.1', 'localhost', '::1', '[::1]')
    if ($AllowLoopbackHttp -and $parsed.Scheme -ceq 'http' -and $parsed.Host -cin $loopbackNames) { return $parsed }
    throw 'release URI must use HTTPS; HTTP is limited to an explicit loopback fixture'
}

function Remove-GoalRouterPartialDownload {
    param(
        [Parameter(Mandatory = $true)][string]$Destination,
        [scriptblock]$TestPathPort,
        [scriptblock]$RemovePathPort
    )
    if ($null -eq $TestPathPort) { $TestPathPort = { param([string]$Path); Test-Path -LiteralPath $Path } }
    if ($null -eq $RemovePathPort) { $RemovePathPort = { param([string]$Path); Remove-Item -LiteralPath $Path -ErrorAction Stop } }
    if (& $TestPathPort -Path $Destination) {
        try { & $RemovePathPort -Path $Destination }
        catch { throw 'release download failed; partial download cleanup failed' }
    }
}

function Get-GoalRouterChecksum {
    param([Parameter(Mandatory = $true)][string]$Text, [Parameter(Mandatory = $true)][string]$AssetName)
    if ($AssetName -cnotmatch '\A[A-Za-z0-9][A-Za-z0-9._-]*\z') { throw 'checksum asset name is invalid' }
    $matches = @()
    foreach ($line in ($Text -split "`r?`n")) {
        if ([string]::IsNullOrEmpty($line)) { continue }
        $match = [regex]::Match($line, '\A([0-9A-Fa-f]{64})(?:  | \*)([^\s]+)\z')
        if ($match.Success -and $match.Groups[2].Value -ceq $AssetName) { $matches += $match.Groups[1].Value.ToLowerInvariant() }
    }
    if ($matches.Count -ne 1) { throw "checksum manifest must contain exactly one valid checksum for $AssetName" }
    return $matches[0]
}

function Assert-GoalRouterZipEntries {
    param([Parameter(Mandatory = $true)][object[]]$Entries)
    $allowed = @('goalrouter.ps1', 'goalrouter.cmd', 'install.ps1', 'uninstall.ps1')
    if ($Entries.Count -ne $allowed.Count) { throw 'ZIP archive members are missing, duplicated, or unexpected' }
    $seen = @{}
    [int64]$totalLength = 0
    foreach ($entry in $Entries) {
        $name = [string]$entry.FullName
        if (-not (Test-GoalRouterLifecycleSingleLine $name)) { throw 'ZIP archive member has an unsafe name' }
        if ($name.StartsWith('/') -or $name.StartsWith('\') -or $name -match '\A[A-Za-z]:' -or $name.Contains('/') -or $name.Contains('\') -or $name.Contains(':') -or $name -in @('.', '..')) {
            throw "ZIP archive member has an unsafe name: $name"
        }
        if ($name -cnotin $allowed) { throw "ZIP archive has an unexpected member: $name" }
        if ($seen.ContainsKey($name)) { throw "ZIP archive has a duplicate member: $name" }
        $seen[$name] = $true
        if ([bool]$entry.IsDirectory) { throw "ZIP archive member is not a regular file: $name" }
        $attributes = [uint32]([int64]$entry.ExternalAttributes -band 0xffffffffL)
        $windowsAttributes = $attributes -band 0xffff
        $unixType = ($attributes -shr 16) -band 0xf000
        if (($windowsAttributes -band 0x450) -ne 0 -or $unixType -notin @(0, 0x8000)) {
            throw "ZIP archive member has an unsafe link, reparse, or directory type: $name"
        }
        $length = [int64]$entry.Length
        $compressedLength = [int64]$entry.CompressedLength
        if ($length -lt 0 -or $length -gt $script:GoalRouterZipEntryLimit -or $compressedLength -lt 0 -or ($length -gt 0 -and $compressedLength -eq 0) -or ($compressedLength -gt 0 -and $length -gt ($compressedLength * $script:GoalRouterZipRatioLimit))) { throw "ZIP archive member has an unsafe size or compression ratio: $name" }
        $totalLength += $length
        if ($totalLength -gt $script:GoalRouterZipTotalLimit) { throw 'ZIP archive has an unsafe total uncompressed size' }
    }
    foreach ($required in $allowed) {
        if (-not $seen.ContainsKey($required)) { throw "ZIP archive is missing member: $required" }
    }
}

function ConvertTo-GoalRouterVersionParts {
    param([Parameter(Mandatory = $true)][string]$Version, [Parameter(Mandatory = $true)][string]$Label)
    if ($Version -cnotmatch '\A[0-9]+(?:\.[0-9]+){1,3}\z') { throw "$Label version is invalid" }
    return @($Version.Split('.') | ForEach-Object { [int64]$_ })
}

function Test-GoalRouterVersionAtLeast {
    param([string]$Actual, [string]$Minimum, [string]$Label)
    $actualParts = @(ConvertTo-GoalRouterVersionParts -Version $Actual -Label $Label)
    $minimumParts = @(ConvertTo-GoalRouterVersionParts -Version $Minimum -Label "minimum $Label")
    $count = [Math]::Max($actualParts.Count, $minimumParts.Count)
    for ($index = 0; $index -lt $count; $index++) {
        $actualPart = if ($index -lt $actualParts.Count) { $actualParts[$index] } else { 0 }
        $minimumPart = if ($index -lt $minimumParts.Count) { $minimumParts[$index] } else { 0 }
        if ($actualPart -gt $minimumPart) { return $true }
        if ($actualPart -lt $minimumPart) { return $false }
    }
    return $true
}

function Assert-GoalRouterExactProperties {
    param([Parameter(Mandatory = $true)]$Value, [Parameter(Mandatory = $true)][string[]]$Names, [Parameter(Mandatory = $true)][string]$Label)
    $actual = @($Value.PSObject.Properties.Name | Sort-Object)
    $expected = @($Names | Sort-Object)
    if ($actual.Count -ne $expected.Count) { throw "$Label schema fields are invalid" }
    for ($index = 0; $index -lt $expected.Count; $index++) {
        if ($actual[$index] -cne $expected[$index]) { throw "$Label schema fields are invalid" }
    }
}

function Assert-GoalRouterReleaseManifest {
    param(
        [Parameter(Mandatory = $true)][string]$Json,
        [Parameter(Mandatory = $true)][string]$RequestedVersion,
        [Parameter(Mandatory = $true)][string]$RequestedImage,
        [Parameter(Mandatory = $true)][string]$Platform,
        [Parameter(Mandatory = $true)][string]$WindowsVersion,
        [Parameter(Mandatory = $true)][string]$PowerShellVersion,
        [Parameter(Mandatory = $true)][string]$WslVersion,
        [Parameter(Mandatory = $true)][string]$DockerClientVersion,
        [Parameter(Mandatory = $true)][string]$DockerServerVersion
    )
    $recordJson = $Json
    if ($recordJson.EndsWith("`r`n", [StringComparison]::Ordinal)) {
        $recordJson = $recordJson.Substring(0, $recordJson.Length - 2)
    }
    elseif ($recordJson.EndsWith("`n", [StringComparison]::Ordinal)) {
        $recordJson = $recordJson.Substring(0, $recordJson.Length - 1)
    }
    if (-not (Test-GoalRouterLifecycleSingleLine $recordJson)) { throw 'release manifest contains invalid bytes' }
    try { $manifest = $recordJson | ConvertFrom-Json -ErrorAction Stop }
    catch { throw 'release manifest JSON is invalid' }
    Assert-GoalRouterExactProperties -Value $manifest -Names @('version', 'protocol_version', 'image', 'image_digest', 'architectures', 'source_revision', 'minimum_hosts') -Label 'release manifest'
    Assert-GoalRouterExactProperties -Value $manifest.minimum_hosts -Names @('windows', 'powershell', 'wsl', 'docker') -Label 'release manifest minimum host'
    if (-not (Test-GoalRouterVersionAtLeast -Actual ([string]$manifest.minimum_hosts.wsl) -Minimum '2.2.3' -Label 'minimum WSL capability')) { throw 'release manifest minimum WSL must support wslinfo version reporting' }
    $canonicalManifest = [ordered]@{
        version = [string]$manifest.version
        protocol_version = [int]$manifest.protocol_version
        image = [string]$manifest.image
        image_digest = [string]$manifest.image_digest
        architectures = @($manifest.architectures | ForEach-Object { [string]$_ })
        source_revision = [string]$manifest.source_revision
        minimum_hosts = [ordered]@{
            windows = [string]$manifest.minimum_hosts.windows
            powershell = [string]$manifest.minimum_hosts.powershell
            wsl = [string]$manifest.minimum_hosts.wsl
            docker = [string]$manifest.minimum_hosts.docker
        }
    } | ConvertTo-Json -Compress -Depth 4
    if ($recordJson -cne $canonicalManifest) { throw 'release manifest is not the canonical deterministic JSON record' }
    if ([string]$manifest.version -cne $RequestedVersion) { throw 'release manifest version does not match request' }
    if ([int]$manifest.protocol_version -ne $script:GoalRouterProtocolVersion) { throw 'release manifest protocol is incompatible' }
    if ([string]$manifest.image -cne $RequestedImage) { throw 'release manifest image does not match request' }
    if (-not (Test-GoalRouterLifecycleDigest ([string]$manifest.image_digest))) { throw 'release manifest image digest is invalid' }
    if (-not (Test-GoalRouterLifecycleSingleLine ([string]$manifest.source_revision)) -or [string]::IsNullOrEmpty([string]$manifest.source_revision)) { throw 'release manifest source revision is invalid' }
    $architectures = @($manifest.architectures)
    if ($architectures.Count -lt 1 -or $Platform -cnotin $architectures) { throw 'release manifest does not support the host platform' }
    $seenArchitectures = @{}
    foreach ($architecture in $architectures) {
        if ([string]$architecture -cnotin @('linux/amd64', 'linux/arm64')) { throw 'release manifest architectures are invalid' }
        if ($seenArchitectures.ContainsKey([string]$architecture)) { throw 'release manifest architectures are duplicated' }
        $seenArchitectures[[string]$architecture] = $true
    }
    foreach ($check in @(
        @{ Actual = $WindowsVersion; Minimum = [string]$manifest.minimum_hosts.windows; Label = 'Windows' },
        @{ Actual = $PowerShellVersion; Minimum = [string]$manifest.minimum_hosts.powershell; Label = 'PowerShell' },
        @{ Actual = $WslVersion; Minimum = [string]$manifest.minimum_hosts.wsl; Label = 'WSL' },
        @{ Actual = $DockerClientVersion; Minimum = [string]$manifest.minimum_hosts.docker; Label = 'Docker client' },
        @{ Actual = $DockerServerVersion; Minimum = [string]$manifest.minimum_hosts.docker; Label = 'Docker daemon' }
    )) {
        if (-not (Test-GoalRouterVersionAtLeast -Actual $check.Actual -Minimum $check.Minimum -Label $check.Label)) { throw "$($check.Label) version is below the release minimum" }
    }
    return $manifest
}

function Copy-GoalRouterPathSnapshot {
    param([Parameter(Mandatory = $true)]$Snapshot)
    return [pscustomobject]@{ Present = [bool]$Snapshot.Present; Value = if ($Snapshot.Present) { [string]$Snapshot.Value } else { $null }; ValueKind = if ($Snapshot.Present -and $Snapshot.PSObject.Properties.Name -contains 'ValueKind') { [string]$Snapshot.ValueKind } elseif ($Snapshot.Present) { 'String' } else { $null } }
}

function Test-GoalRouterWindowsPathEquivalent {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$First, [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Second)
    return $First.Replace('/', '\').TrimEnd('\') -ieq $Second.Replace('/', '\').TrimEnd('\')
}

function Test-GoalRouterWindowsPathContainsOrEqual {
    param([Parameter(Mandatory = $true)][string]$Parent, [Parameter(Mandatory = $true)][string]$Child)
    $canonicalParent = $Parent.Replace('/', '\').TrimEnd('\')
    $canonicalChild = $Child.Replace('/', '\').TrimEnd('\')
    return $canonicalChild -ieq $canonicalParent -or $canonicalChild.StartsWith($canonicalParent + '\', [StringComparison]::OrdinalIgnoreCase)
}

function Add-GoalRouterUserPathEntry {
    param([Parameter(Mandatory = $true)]$Snapshot, [Parameter(Mandatory = $true)][string]$OwnedEntry, [switch]$NoPathUpdate)
    if (-not (Test-GoalRouterLifecyclePathText $OwnedEntry) -or $OwnedEntry.Contains(';')) { throw 'owned User PATH entry is invalid' }
    $before = Copy-GoalRouterPathSnapshot -Snapshot $Snapshot
    $beforeState = if (-not $before.Present) { 'absent' } elseif ([string]::IsNullOrEmpty([string]$before.Value)) { 'empty' } else { 'populated' }
    if ($NoPathUpdate) { return [pscustomobject]@{ Changed = $false; InstallerAdded = $false; UpdateEnabled = $false; OwnedValue = $OwnedEntry; Snapshot = $before; Before = $before; BeforeState = $beforeState; BeforeValueKind = $before.ValueKind; AfterValueKind = $before.ValueKind; AfterSha256 = if ($before.Present) { Get-GoalRouterStringSha256 ([string]$before.Value) } else { $null } } }
    $segments = if ($before.Present) { @(([string]$before.Value).Split(';')) } else { @() }
    foreach ($segment in $segments) {
        if (Test-GoalRouterWindowsPathEquivalent -First $segment -Second $OwnedEntry) { return [pscustomobject]@{ Changed = $false; InstallerAdded = $false; UpdateEnabled = $true; OwnedValue = $OwnedEntry; Snapshot = $before; Before = $before; BeforeState = $beforeState; BeforeValueKind = $before.ValueKind; AfterValueKind = $before.ValueKind; AfterSha256 = Get-GoalRouterStringSha256 ([string]$before.Value) } }
    }
    $newValue = if (-not $before.Present -or [string]::IsNullOrEmpty([string]$before.Value)) { $OwnedEntry } else { [string]$before.Value + ';' + $OwnedEntry }
    $after = [pscustomobject]@{ Present = $true; Value = $newValue; ValueKind = if ($before.Present) { [string]$before.ValueKind } else { 'String' } }
    return [pscustomobject]@{ Changed = $true; InstallerAdded = $true; UpdateEnabled = $true; OwnedValue = $OwnedEntry; Snapshot = $after; Before = $before; BeforeState = $beforeState; BeforeValueKind = $before.ValueKind; AfterValueKind = $after.ValueKind; AfterSha256 = Get-GoalRouterStringSha256 $newValue }
}

function Remove-GoalRouterUserPathEntry {
    param([Parameter(Mandatory = $true)]$Snapshot, [Parameter(Mandatory = $true)]$Ownership)
    $current = Copy-GoalRouterPathSnapshot -Snapshot $Snapshot
    if (-not [bool]$Ownership.InstallerAdded) { return [pscustomobject]@{ Changed = $false; Snapshot = $current } }
    if (-not $current.Present -or [string]$current.ValueKind -cne [string]$Ownership.AfterValueKind -or (Get-GoalRouterStringSha256 ([string]$current.Value)) -cne [string]$Ownership.AfterSha256) { return [pscustomobject]@{ Changed = $false; Snapshot = $current } }
    $owned = [string]$Ownership.OwnedValue
    if ([string]$current.Value -ceq $owned -and [string]$Ownership.BeforeState -cin @('absent', 'empty')) {
        $prior = if ([string]$Ownership.BeforeState -ceq 'absent') { [pscustomobject]@{ Present = $false; Value = $null; ValueKind = $null } } else { [pscustomobject]@{ Present = $true; Value = ''; ValueKind = [string]$Ownership.BeforeValueKind } }
        return [pscustomobject]@{ Changed = $true; Snapshot = $prior }
    }
    $suffix = ';' + $owned
    if ([string]$Ownership.BeforeState -cne 'populated' -or -not ([string]$current.Value).EndsWith($suffix, [StringComparison]::Ordinal)) { return [pscustomobject]@{ Changed = $false; Snapshot = $current } }
    $priorValue = ([string]$current.Value).Substring(0, ([string]$current.Value).Length - $suffix.Length)
    return [pscustomobject]@{ Changed = $true; Snapshot = [pscustomobject]@{ Present = $true; Value = $priorValue; ValueKind = [string]$Ownership.BeforeValueKind } }
}

function ConvertTo-GoalRouterCanonicalJson {
    param([Parameter(Mandatory = $true)]$Value)
    $json = $Value | ConvertTo-Json -Compress -Depth 10
    if (-not (Test-GoalRouterLifecyclePathText $json)) { throw 'canonical manifest serialization failed' }
    return $json
}

function ConvertTo-GoalRouterCanonicalInstallManifestJson {
    param([Parameter(Mandatory = $true)]$Manifest)
    $canonical = [ordered]@{
        manifest_version = [int]$Manifest.manifest_version
        protocol_version = [int]$Manifest.protocol_version
        version = [string]$Manifest.version
        launcher_version = [string]$Manifest.launcher_version
        image_reference = [string]$Manifest.image_reference
        image_digest = [string]$Manifest.image_digest
        image_platform = [string]$Manifest.image_platform
        source_revision = [string]$Manifest.source_revision
        owned = [ordered]@{
            launcher = [string]$Manifest.owned.launcher
            cmd = [string]$Manifest.owned.cmd
            installer = [string]$Manifest.owned.installer
            uninstaller = [string]$Manifest.owned.uninstaller
            install_root = [string]$Manifest.owned.install_root
            bin_dir = [string]$Manifest.owned.bin_dir
            config_file = [string]$Manifest.owned.config_file
            config_dir = [string]$Manifest.owned.config_dir
            state_dir = [string]$Manifest.owned.state_dir
            codex_home = [string]$Manifest.owned.codex_home
        }
        wsl_distribution = [string]$Manifest.wsl_distribution
        path_ownership = [ordered]@{
            installer_added = $Manifest.path_ownership.installer_added
            update_enabled = $Manifest.path_ownership.update_enabled
            owned_value = [string]$Manifest.path_ownership.owned_value
            before_state = [string]$Manifest.path_ownership.before_state
            before_value_kind = $Manifest.path_ownership.before_value_kind
            after_value_kind = $Manifest.path_ownership.after_value_kind
            after_sha256 = $Manifest.path_ownership.after_sha256
        }
        release_base = [string]$Manifest.release_base
    }
    return ConvertTo-GoalRouterCanonicalJson $canonical
}

function Get-GoalRouterStringSha256 {
    param([Parameter(Mandatory = $true)][string]$Value)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { $hash = $algorithm.ComputeHash([Text.UTF8Encoding]::new($false).GetBytes($Value)) }
    finally { $algorithm.Dispose() }
    return ([BitConverter]::ToString($hash)).Replace('-', '').ToLowerInvariant()
}

function ConvertFrom-GoalRouterStrictUtf8Bytes {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes, [Parameter(Mandatory = $true)][string]$Label)
    if ($Bytes.Count -ge 3 -and $Bytes[0] -eq 0xef -and $Bytes[1] -eq 0xbb -and $Bytes[2] -eq 0xbf) { throw "$Label must be BOM-less UTF-8" }
    try { $text = [Text.UTF8Encoding]::new($false, $true).GetString($Bytes) }
    catch { throw "$Label contains invalid UTF-8 bytes" }
    $canonicalBytes = [Text.UTF8Encoding]::new($false, $true).GetBytes($text)
    if ($canonicalBytes.Count -ne $Bytes.Count) { throw "$Label is not canonical UTF-8" }
    for ($index = 0; $index -lt $Bytes.Count; $index++) { if ($canonicalBytes[$index] -ne $Bytes[$index]) { throw "$Label is not canonical UTF-8" } }
    return $text
}

function New-GoalRouterInstallManifest {
    param(
        [Parameter(Mandatory = $true)][string]$Version,
        [Parameter(Mandatory = $true)][string]$ImageReference,
        [Parameter(Mandatory = $true)][string]$ImageDigest,
        [Parameter(Mandatory = $true)][string]$ImagePlatform,
        [Parameter(Mandatory = $true)][string]$SourceRevision,
        [Parameter(Mandatory = $true)][string]$WslDistribution,
        [Parameter(Mandatory = $true)]$Layout,
        [Parameter(Mandatory = $true)]$PathOwnership,
        [Parameter(Mandatory = $true)][string]$ReleaseBase
    )
    if (-not (Test-GoalRouterLifecycleDigest $ImageDigest)) { throw 'install manifest image digest is invalid' }
    $beforeState = if ($PathOwnership.PSObject.Properties.Name -contains 'BeforeState') { [string]$PathOwnership.BeforeState } elseif (-not [bool]$PathOwnership.Before.Present) { 'absent' } elseif ([string]::IsNullOrEmpty([string]$PathOwnership.Before.Value)) { 'empty' } else { 'populated' }
    $beforeValueKind = if ($PathOwnership.PSObject.Properties.Name -contains 'BeforeValueKind') { $PathOwnership.BeforeValueKind } elseif ($beforeState -ceq 'absent') { $null } elseif ($PathOwnership.Before.PSObject.Properties.Name -contains 'ValueKind') { [string]$PathOwnership.Before.ValueKind } else { 'String' }
    $afterSnapshot = if ($PathOwnership.PSObject.Properties.Name -contains 'After') { $PathOwnership.After } else { $PathOwnership.Snapshot }
    $afterSha256 = if ($PathOwnership.PSObject.Properties.Name -contains 'AfterSha256') { $PathOwnership.AfterSha256 } elseif ([bool]$afterSnapshot.Present) { Get-GoalRouterStringSha256 ([string]$afterSnapshot.Value) } else { $null }
    $afterValueKind = if ($PathOwnership.PSObject.Properties.Name -contains 'AfterValueKind') { $PathOwnership.AfterValueKind } elseif ([bool]$afterSnapshot.Present -and $afterSnapshot.PSObject.Properties.Name -contains 'ValueKind') { [string]$afterSnapshot.ValueKind } elseif ([bool]$afterSnapshot.Present) { 'String' } else { $null }
    return [ordered]@{
        manifest_version = 1
        protocol_version = $script:GoalRouterProtocolVersion
        version = $Version
        launcher_version = $Version
        image_reference = $ImageReference
        image_digest = $ImageDigest
        image_platform = $ImagePlatform
        source_revision = $SourceRevision
        owned = [ordered]@{
            launcher = $Layout.LauncherPath
            cmd = $Layout.CmdPath
            installer = $Layout.InstallerPath
            uninstaller = $Layout.UninstallerPath
            install_root = $Layout.InstallRoot
            bin_dir = $Layout.BinDir
            config_file = $Layout.ConfigFile
            config_dir = $Layout.ConfigDir
            state_dir = $Layout.StateDir
            codex_home = $Layout.CodexHome
        }
        wsl_distribution = $WslDistribution
        path_ownership = [ordered]@{
            installer_added = [bool]$PathOwnership.InstallerAdded
            update_enabled = if ($PathOwnership.PSObject.Properties.Name -contains 'UpdateEnabled') { [bool]$PathOwnership.UpdateEnabled } else { [bool]$PathOwnership.InstallerAdded }
            owned_value = [string]$PathOwnership.OwnedValue
            before_state = $beforeState
            before_value_kind = $beforeValueKind
            after_value_kind = $afterValueKind
            after_sha256 = if ([string]::IsNullOrEmpty([string]$afterSha256)) { $null } else { [string]$afterSha256 }
        }
        release_base = $ReleaseBase
    }
}

function Assert-GoalRouterExistingInstallManifest {
    param(
        [Parameter(Mandatory = $true)]$Manifest,
        [Parameter(Mandatory = $true)][string]$Json,
        [Parameter(Mandatory = $true)]$Layout
    )
    Assert-GoalRouterExactProperties -Value $Manifest -Names @('manifest_version', 'protocol_version', 'version', 'launcher_version', 'image_reference', 'image_digest', 'image_platform', 'source_revision', 'owned', 'wsl_distribution', 'path_ownership', 'release_base') -Label 'existing install control'
    Assert-GoalRouterExactProperties -Value $Manifest.owned -Names @('launcher', 'cmd', 'installer', 'uninstaller', 'install_root', 'bin_dir', 'config_file', 'config_dir', 'state_dir', 'codex_home') -Label 'existing owned layout'
    Assert-GoalRouterExactProperties -Value $Manifest.path_ownership -Names @('installer_added', 'update_enabled', 'owned_value', 'before_state', 'before_value_kind', 'after_value_kind', 'after_sha256') -Label 'existing PATH ownership'
    if ((ConvertTo-GoalRouterCanonicalInstallManifestJson $Manifest) -cne $Json) { throw 'existing install control is not canonical' }
    if ([int]$Manifest.manifest_version -ne 1 -or [int]$Manifest.protocol_version -ne $script:GoalRouterProtocolVersion -or [string]$Manifest.version -cnotmatch '\A[0-9]+\.[0-9]+\.[0-9]+\z' -or [string]$Manifest.launcher_version -cne [string]$Manifest.version) { throw 'existing install control version or protocol is invalid' }
    $existingImageReference = [string]$Manifest.image_reference
    $namedImageIsValid = $existingImageReference -cmatch '\A(?:[a-z0-9]+(?:(?:[.]|__|_|-+)[a-z0-9]+)*/)*(?:[a-z0-9]+(?:(?:[.]|__|_|-+)[a-z0-9]+)*)\z' -or $existingImageReference -cmatch '\A(?:[A-Za-z0-9.-]+(?::[0-9]+)?/)+(?:[a-z0-9]+(?:(?:[.]|__|_|-+)[a-z0-9]+)*)\z'
    if (-not (Test-GoalRouterLifecycleDigest ([string]$Manifest.image_digest)) -or -not $namedImageIsValid -or [string]$Manifest.image_platform -cnotin @('linux/amd64', 'linux/arm64') -or -not (Test-GoalRouterLifecycleSingleLine ([string]$Manifest.source_revision)) -or -not (Test-GoalRouterWslDistribution ([string]$Manifest.wsl_distribution))) { throw 'existing install control runtime authority is invalid' }
    [void](Assert-GoalRouterReleaseUri -Uri ([string]$Manifest.release_base) -AllowLoopbackHttp ([string]$Manifest.release_base -cmatch '\Ahttp://(?:127\.0\.0\.1|localhost|\[?::1\]?)'))
    foreach ($pair in @(
        @([string]$Manifest.owned.install_root, [string]$Layout.InstallRoot),
        @([string]$Manifest.owned.bin_dir, [string]$Layout.BinDir),
        @([string]$Manifest.owned.config_file, [string]$Layout.ConfigFile),
        @([string]$Manifest.owned.config_dir, [string]$Layout.ConfigDir),
        @([string]$Manifest.owned.state_dir, [string]$Layout.StateDir),
        @([string]$Manifest.owned.codex_home, [string]$Layout.CodexHome),
        @([string]$Manifest.owned.launcher, [string]$Layout.LauncherPath),
        @([string]$Manifest.owned.cmd, [string]$Layout.CmdPath),
        @([string]$Manifest.owned.installer, [string]$Layout.InstallerPath),
        @([string]$Manifest.owned.uninstaller, [string]$Layout.UninstallerPath),
        @([string]$Manifest.path_ownership.owned_value, [string]$Layout.BinDir)
    )) {
        if (-not (Test-GoalRouterWindowsPathEquivalent -First ([string]$pair[0]) -Second ([string]$pair[1]))) { throw 'existing install control path relationships are invalid' }
    }
    foreach ($name in @('installer_added', 'update_enabled')) {
        if ($Manifest.path_ownership.$name -isnot [bool]) { throw 'existing PATH ownership flag is invalid' }
    }
    if ([string]$Manifest.path_ownership.before_state -cnotin @('absent', 'empty', 'populated')) { throw 'existing PATH ownership state is invalid' }
    if (([string]$Manifest.path_ownership.before_state -ceq 'absent' -and $null -ne $Manifest.path_ownership.before_value_kind) -or ([string]$Manifest.path_ownership.before_state -cne 'absent' -and [string]$Manifest.path_ownership.before_value_kind -cnotin @('String', 'ExpandString'))) { throw 'existing PATH ownership value kind is invalid' }
    $afterKindPresent = $null -ne $Manifest.path_ownership.after_value_kind
    $afterHashPresent = $null -ne $Manifest.path_ownership.after_sha256
    if ($afterKindPresent -ne $afterHashPresent) { throw 'existing PATH ownership post-install state is incomplete' }
    if ($afterKindPresent -and [string]$Manifest.path_ownership.after_value_kind -cnotin @('String', 'ExpandString')) { throw 'existing PATH ownership post-install value kind is invalid' }
    if ($afterHashPresent -and [string]$Manifest.path_ownership.after_sha256 -cnotmatch '\A[0-9a-f]{64}\z') { throw 'existing PATH ownership hash is invalid' }
    $installerAdded = [bool]$Manifest.path_ownership.installer_added
    $updateEnabled = [bool]$Manifest.path_ownership.update_enabled
    $beforeState = [string]$Manifest.path_ownership.before_state
    if ($installerAdded -and (-not $updateEnabled -or -not $afterHashPresent)) { throw 'existing PATH ownership added state is invalid' }
    if (-not $afterHashPresent -and ($beforeState -cne 'absent' -or $installerAdded -or $updateEnabled)) { throw 'existing PATH ownership absent post-install state is invalid' }
    if (-not $installerAdded -and $updateEnabled -and $beforeState -cne 'populated') { throw 'existing PATH ownership pre-existing entry state is invalid' }
    if (-not $installerAdded -and -not $updateEnabled -and $afterHashPresent -and $beforeState -ceq 'absent') { throw 'existing PATH ownership preserved state is invalid' }
    if ($installerAdded -and $beforeState -ceq 'absent' -and [string]$Manifest.path_ownership.after_value_kind -cne 'String') { throw 'existing PATH ownership created value kind is invalid' }
    if ($beforeState -cne 'absent' -and $afterKindPresent -and [string]$Manifest.path_ownership.after_value_kind -cne [string]$Manifest.path_ownership.before_value_kind) { throw 'existing PATH ownership value kind transition is invalid' }
}

function Get-GoalRouterImageRepository {
    param([Parameter(Mandatory = $true)][string]$Image)
    $withoutDigest = $Image.Split('@')[0]
    $lastSlash = $withoutDigest.LastIndexOf('/')
    $lastColon = $withoutDigest.LastIndexOf(':')
    if ($lastColon -gt $lastSlash) { return $withoutDigest.Substring(0, $lastColon) }
    return $withoutDigest
}

function Invoke-GoalRouterLifecycleNative {
    param([scriptblock]$NativeInvoker, [string]$Distribution, [string[]]$Arguments, [bool]$CaptureOutput = $true)
    $wslArguments = New-GoalRouterWslArguments -Distribution $Distribution -Arguments $Arguments
    $result = & $NativeInvoker -FilePath 'wsl.exe' -Arguments $wslArguments -CaptureOutput $CaptureOutput
    if ([int]$result.ExitCode -ne 0) { throw "native prerequisite or candidate command failed with exit code $($result.ExitCode)" }
    return $result
}

function Test-GoalRouterCandidateImage {
    param(
        [Parameter(Mandatory = $true)]$Manifest,
        [Parameter(Mandatory = $true)][string]$Distribution,
        [Parameter(Mandatory = $true)][string]$Platform,
        [Parameter(Mandatory = $true)][scriptblock]$NativeInvoker
    )
    [void](Invoke-GoalRouterLifecycleNative -NativeInvoker $NativeInvoker -Distribution $Distribution -Arguments @('docker', 'pull', [string]$Manifest.image))
    $digestResult = Invoke-GoalRouterLifecycleNative -NativeInvoker $NativeInvoker -Distribution $Distribution -Arguments @('docker', 'image', 'inspect', '--format', '{{range .RepoDigests}}{{println .}}{{end}}', [string]$Manifest.image)
    $repository = Get-GoalRouterImageRepository -Image ([string]$Manifest.image)
    $repoDigests = @($digestResult.Output)
    if ($repoDigests.Count -ne 1 -or [string]$repoDigests[0] -cnotmatch ('\A' + [regex]::Escape($repository) + '@sha256:[0-9a-f]{64}\z')) { throw 'candidate image must have one canonical repository digest' }
    $repoDigest = [string]$repoDigests[0]
    $actualDigest = $repoDigest.Substring($repoDigest.IndexOf('@') + 1)
    if ($actualDigest -cne [string]$Manifest.image_digest) { throw 'candidate image digest does not match trusted release manifest' }
    $architectureResult = Invoke-GoalRouterLifecycleNative -NativeInvoker $NativeInvoker -Distribution $Distribution -Arguments @('docker', 'image', 'inspect', '--format', '{{.Architecture}}', $repoDigest)
    $expectedArchitecture = if ($Platform -ceq 'linux/amd64') { 'amd64' } elseif ($Platform -ceq 'linux/arm64') { 'arm64' } else { throw 'unsupported Windows runtime platform' }
    $architecture = @($architectureResult.Output)
    if ($architecture.Count -ne 1 -or [string]$architecture[0] -cne $expectedArchitecture) { throw 'candidate image platform does not match trusted release manifest' }
    $revisionResult = Invoke-GoalRouterLifecycleNative -NativeInvoker $NativeInvoker -Distribution $Distribution -Arguments @('docker', 'image', 'inspect', '--format', '{{index .Config.Labels "org.opencontainers.image.revision"}}', $repoDigest)
    $revisions = @($revisionResult.Output)
    if ($revisions.Count -ne 1 -or [string]$revisions[0] -cne [string]$Manifest.source_revision) { throw 'candidate image revision does not match trusted release manifest' }
    $versionResult = Invoke-GoalRouterLifecycleNative -NativeInvoker $NativeInvoker -Distribution $Distribution -Arguments @('docker', 'run', '--rm', '--read-only', '--tmpfs', '/tmp:rw,exec,nosuid,size=64m,mode=1777', $repoDigest, '--json', 'version')
    $versionLines = @($versionResult.Output)
    if ($versionLines.Count -ne 1) { throw 'candidate image version output is invalid' }
    try { $runtime = [string]$versionLines[0] | ConvertFrom-Json -ErrorAction Stop }
    catch { throw 'candidate image version output is invalid' }
    if ([string]$runtime.version -cne [string]$Manifest.version) { throw 'candidate application version does not match release manifest' }
    if ([int]$runtime.protocol_version -ne [int]$Manifest.protocol_version) { throw 'candidate protocol does not match release manifest' }
    return [pscustomobject]@{ RepoDigest = $repoDigest; ImageDigest = $actualDigest; Revision = [string]$revisions[0]; Runtime = $runtime }
}

function Get-GoalRouterWindowsPathSecurity {
    param([Parameter(Mandatory = $true)][string]$Path)
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
    $ownerSid = if ([string]$acl.Owner -match '\AS-\d(?:-\d+)+\z') { [string]$acl.Owner } else { ([Security.Principal.NTAccount]$acl.Owner).Translate([Security.Principal.SecurityIdentifier]).Value }
    $allowedSids = @($identity.User.Value, 'S-1-5-18', 'S-1-5-32-544', 'S-1-3-0')
    $unsafe = @($acl.Access | Where-Object {
        $sid = $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        $_.AccessControlType -ceq [Security.AccessControl.AccessControlType]::Allow -and (Test-GoalRouterLifecycleAclRightsUnsafe -Rights ([long]$_.FileSystemRights)) -and $sid -notin $allowedSids
    }).Count -gt 0
    return [pscustomobject]@{ OwnerMatchesCurrentUser = $ownerSid -ceq $identity.User.Value; OwnerIsTrusted = $ownerSid -in $allowedSids; AclIsSafe = -not $unsafe }
}

function Get-GoalRouterLifecycleMutationRightsMask {
    return [long]([Security.AccessControl.FileSystemRights]::WriteData -bor [Security.AccessControl.FileSystemRights]::AppendData -bor [Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor [Security.AccessControl.FileSystemRights]::WriteAttributes -bor [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor [Security.AccessControl.FileSystemRights]::Delete -bor [Security.AccessControl.FileSystemRights]::ChangePermissions -bor [Security.AccessControl.FileSystemRights]::TakeOwnership)
}

function Test-GoalRouterLifecycleAclRightsUnsafe {
    param([Parameter(Mandatory = $true)][long]$Rights)
    return ($Rights -band (Get-GoalRouterLifecycleMutationRightsMask)) -ne 0
}

function Ensure-GoalRouterDirectoryChain {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][scriptblock]$TestDirectory,
        [Parameter(Mandatory = $true)][scriptblock]$CreateDirectory,
        [Parameter(Mandatory = $true)][scriptblock]$GetAttributes,
        [Parameter(Mandatory = $true)][scriptblock]$RemoveDirectory
    )
    if (& $TestDirectory -Path $Path) {
        if ((& $GetAttributes -Path $Path) -band [IO.FileAttributes]::ReparsePoint) { throw "owned directory is a reparse point: $Path" }
        return @()
    }
    $missing = [System.Collections.ArrayList]::new()
    $cursor = $Path
    while (-not (& $TestDirectory -Path $cursor)) {
        [void]$missing.Add($cursor)
        $parent = Split-Path -Parent $cursor
        if ([string]::IsNullOrEmpty($parent) -or $parent -ceq $cursor) { throw "cannot find an existing parent for owned directory: $Path" }
        $cursor = $parent
    }
    $created = [System.Collections.ArrayList]::new()
    try {
        for ($index = $missing.Count - 1; $index -ge 0; $index--) {
            $createdPath = [string]$missing[$index]
            & $CreateDirectory -Path $createdPath
            [void]$created.Add($createdPath)
            if ((& $GetAttributes -Path $createdPath) -band [IO.FileAttributes]::ReparsePoint) { throw "owned directory is a reparse point: $createdPath" }
        }
    } catch {
        $failure = $_
        $cleanupFailures = [System.Collections.ArrayList]::new()
        for ($index = $created.Count - 1; $index -ge 0; $index--) {
            try { & $RemoveDirectory -Path ([string]$created[$index]) }
            catch { [void]$cleanupFailures.Add($_.Exception.Message) }
        }
        if ($cleanupFailures.Count -gt 0) { throw "$($failure.Exception.Message); directory creation rollback failures: $($cleanupFailures -join '; ')" }
        throw $failure
    }
    return $created.ToArray()
}

function New-GoalRouterProductionLifecyclePorts {
    $native = {
        param([string]$FilePath, [string[]]$Arguments, [bool]$CaptureOutput)
        $output = @(& $FilePath @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
        $nativeErrors = @($output | Where-Object { $_ -is [Management.Automation.ErrorRecord] })
        if ($exitCode -eq 0 -and $nativeErrors.Count -gt 0) { throw 'native command emitted stderr despite a successful exit code' }
        if (-not $CaptureOutput) { foreach ($line in $output) { [Console]::Out.WriteLine($line) } }
        return [pscustomobject]@{ ExitCode = $exitCode; Output = if ($CaptureOutput) { $output } else { $null } }
    }
    $download = {
        param([string]$Uri, [string]$Destination, [bool]$AllowLoopbackHttp)
        Add-Type -AssemblyName System.Net.Http -ErrorAction Stop
        $handler = [Net.Http.HttpClientHandler]::new()
        $handler.AllowAutoRedirect = $false
        $client = [Net.Http.HttpClient]::new($handler)
        $current = Assert-GoalRouterReleaseUri -Uri $Uri -AllowLoopbackHttp $AllowLoopbackHttp
        try {
            for ($redirect = 0; $redirect -le 5; $redirect++) {
                $response = $client.GetAsync($current).GetAwaiter().GetResult()
                try {
                    $status = [int]$response.StatusCode
                    if ($status -in @(301, 302, 303, 307, 308)) {
                        if ($redirect -eq 5 -or $null -eq $response.Headers.Location) { throw 'redirect limit' }
                        $next = if ($response.Headers.Location.IsAbsoluteUri) { $response.Headers.Location } else { [Uri]::new($current, $response.Headers.Location) }
                        $current = Assert-GoalRouterReleaseUri -Uri $next.AbsoluteUri -AllowLoopbackHttp $AllowLoopbackHttp -AllowRedirectQuery $true
                        continue
                    }
                    if (-not $response.IsSuccessStatusCode) { throw 'HTTP status failure' }
                    $input = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
                    try {
                        $output = [IO.File]::Open($Destination, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
                        try { $input.CopyTo($output) } finally { $output.Dispose() }
                    } finally { $input.Dispose() }
                    return
                } finally { $response.Dispose() }
            }
        } catch {
            Remove-GoalRouterPartialDownload -Destination $Destination
            throw 'release download failed'
        } finally { $client.Dispose(); $handler.Dispose() }
    }
    $resolveLatestVersion = {
        Add-Type -AssemblyName System.Net.Http -ErrorAction Stop
        $handler = [Net.Http.HttpClientHandler]::new()
        $handler.AllowAutoRedirect = $false
        $client = [Net.Http.HttpClient]::new($handler)
        $current = Assert-GoalRouterReleaseUri -Uri 'https://github.com/vparla/GoalRouter/releases/latest' -AllowLoopbackHttp $false
        try {
            for ($redirect = 0; $redirect -le 5; $redirect++) {
                $response = $client.GetAsync($current).GetAwaiter().GetResult()
                try {
                    $status = [int]$response.StatusCode
                    if ($status -in @(301, 302, 303, 307, 308)) {
                        if ($redirect -eq 5 -or $null -eq $response.Headers.Location) { throw 'latest release redirect limit' }
                        $next = if ($response.Headers.Location.IsAbsoluteUri) { $response.Headers.Location } else { [Uri]::new($current, $response.Headers.Location) }
                        $current = Assert-GoalRouterReleaseUri -Uri $next.AbsoluteUri -AllowLoopbackHttp $false
                        continue
                    }
                    if (-not $response.IsSuccessStatusCode) { throw 'latest release HTTP status failure' }
                    $match = [regex]::Match($current.AbsoluteUri, '\Ahttps://github\.com/vparla/GoalRouter/releases/tag/v([0-9]+\.[0-9]+\.[0-9]+)\z')
                    if (-not $match.Success) { throw 'latest release target is invalid' }
                    return [string]$match.Groups[1].Value
                } finally { $response.Dispose() }
            }
        } catch { throw 'cannot resolve latest stable release' }
        finally { $client.Dispose(); $handler.Dispose() }
    }
    $getUserPath = {
        $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $false)
        if ($null -eq $key) { return [pscustomobject]@{ Present = $false; Value = $null; ValueKind = $null } }
        try {
            if (@($key.GetValueNames()) -notcontains 'Path') { return [pscustomobject]@{ Present = $false; Value = $null; ValueKind = $null } }
            $kind = $key.GetValueKind('Path').ToString()
            if ($kind -cnotin @('String', 'ExpandString')) { throw 'User PATH registry value kind is unsupported' }
            $value = [string]$key.GetValue('Path', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            return [pscustomobject]@{ Present = $true; Value = $value; ValueKind = $kind }
        } finally { $key.Dispose() }
    }
    if ($null -eq ('GoalRouter.EnvironmentBroadcaster' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
namespace GoalRouter {
    public static class EnvironmentBroadcaster {
        private const int HWND_BROADCAST = 0xffff;
        private const int WM_SETTINGCHANGE = 0x001a;
        private const int SMTO_ABORTIFHUNG = 0x0002;
        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true, ExactSpelling = true)]
        private static extern IntPtr SendMessageTimeoutW(IntPtr window, int message, IntPtr wParam, string lParam, int flags, int timeout, out IntPtr result);
        public static void Broadcast() {
            IntPtr result;
            IntPtr status = SendMessageTimeoutW((IntPtr)HWND_BROADCAST, WM_SETTINGCHANGE, IntPtr.Zero, "Environment", SMTO_ABORTIFHUNG, 5000, out result);
            if (status == IntPtr.Zero) {
                int error = Marshal.GetLastWin32Error();
                if (error != 0) throw new Win32Exception(error);
                throw new TimeoutException("environment change broadcast timed out without a Win32 error code");
            }
        }
    }
}
'@ -ErrorAction Stop
    }
    $setUserPath = {
        param($Snapshot)
        $before = & $getUserPath
        $writeRegistrySnapshot = {
            param($ValueSnapshot)
            $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
            if ($null -eq $key) { throw 'User environment registry key is unavailable' }
            try {
                if ([bool]$ValueSnapshot.Present) {
                    $kindName = if ($ValueSnapshot.PSObject.Properties.Name -contains 'ValueKind') { [string]$ValueSnapshot.ValueKind } else { 'String' }
                    if ($kindName -cnotin @('String', 'ExpandString')) { throw 'User PATH registry value kind is unsupported' }
                    $kind = [Microsoft.Win32.RegistryValueKind][Enum]::Parse([Microsoft.Win32.RegistryValueKind], $kindName, $false)
                    $key.SetValue('Path', [string]$ValueSnapshot.Value, $kind)
                } else { $key.DeleteValue('Path', $false) }
            } finally { $key.Dispose() }
        }
        try {
            & $writeRegistrySnapshot -ValueSnapshot $Snapshot
            [GoalRouter.EnvironmentBroadcaster]::Broadcast()
        } catch {
            $failure = $_
            & $writeRegistrySnapshot -ValueSnapshot $before
            try { [GoalRouter.EnvironmentBroadcaster]::Broadcast() }
            catch { throw "$($failure.Exception.Message); User PATH rollback broadcast failed: $($_.Exception.Message)" }
            throw $failure
        }
    }
    $getHost = {
        if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'install.ps1 requires Windows' }
        if ([string]::IsNullOrEmpty($env:LOCALAPPDATA) -or [string]::IsNullOrEmpty($env:APPDATA) -or [string]::IsNullOrEmpty($env:USERPROFILE)) { throw 'Windows profile and AppData variables are required' }
        $architecture = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
        $platform = if ($architecture -ceq 'X64') { 'linux/amd64' } elseif ($architecture -ceq 'Arm64') { 'linux/arm64' } else { throw "unsupported Windows architecture: $architecture" }
        return [pscustomobject]@{ LocalAppData = $env:LOCALAPPDATA; AppData = $env:APPDATA; UserProfile = $env:USERPROFILE; WindowsVersion = [Environment]::OSVersion.Version.ToString(); PowerShellVersion = $PSVersionTable.PSVersion.ToString(); Platform = $platform }
    }
    $resolvePath = {
        param([string]$Path, [string]$Kind, [bool]$AllowMissing)
        $exists = Test-Path -LiteralPath $Path
        if (-not $exists -and -not $AllowMissing) { throw "$Kind path does not exist: $Path" }
        $probe = $Path
        while (-not (Test-Path -LiteralPath $probe)) {
            $parent = Split-Path -Parent $probe
            if ([string]::IsNullOrEmpty($parent) -or $parent -ceq $probe) { throw "cannot resolve destination path: $Path" }
            $probe = $parent
        }
        $resolved = @(Resolve-Path -LiteralPath $probe -ErrorAction Stop)
        if ($resolved.Count -ne 1) { throw "path must resolve exactly once: $Path" }
        $ancestorProviderPath = [string]$resolved[0].ProviderPath
        if (-not (Test-GoalRouterWindowsPathEquivalent -First $ancestorProviderPath -Second $probe)) { throw "path resolves through a redirected FileSystem provider: $Path" }
        $providerPath = if ($exists) { $ancestorProviderPath } else { $Path }
        $attributes = if ($exists) { [IO.File]::GetAttributes($providerPath) } else { [IO.FileAttributes]0 }
        $parentReparse = $false
        $ancestorChainIsSafe = $true
        $cursor = if ($exists) { $providerPath } else { $probe }
        while (-not [string]::IsNullOrEmpty($cursor)) {
            if ($cursor.TrimEnd('\', '/') -cmatch '\A[A-Za-z]:\z') { break }
            if (([IO.File]::GetAttributes($cursor) -band [IO.FileAttributes]::ReparsePoint) -ne 0) { $parentReparse = $true; break }
            $cursorSecurity = Get-GoalRouterWindowsPathSecurity -Path $cursor
            if (-not [bool]$cursorSecurity.OwnerIsTrusted -or -not [bool]$cursorSecurity.AclIsSafe) { $ancestorChainIsSafe = $false }
            $next = Split-Path -Parent $cursor
            if ($next -ceq $cursor) { break }
            $cursor = $next
        }
        $pathSecurity = Get-GoalRouterWindowsPathSecurity -Path $ancestorProviderPath
        return [pscustomobject]@{ Path = $Path; ProviderName = $resolved[0].Provider.Name; ProviderPath = $providerPath; AncestorProviderPath = $ancestorProviderPath; Exists = $exists; IsContainer = $exists -and (Test-Path -LiteralPath $providerPath -PathType Container); IsLeaf = $exists -and (Test-Path -LiteralPath $providerPath -PathType Leaf); IsReparsePoint = (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0); ParentIsReparsePoint = $parentReparse; OwnerMatchesCurrentUser = [bool]$pathSecurity.OwnerMatchesCurrentUser; AclIsSafe = [bool]$pathSecurity.AclIsSafe; AncestorOwnerMatchesCurrentUser = [bool]$pathSecurity.OwnerMatchesCurrentUser; AncestorAclIsSafe = [bool]$pathSecurity.AclIsSafe; AncestorChainIsSafe = $ancestorChainIsSafe }
    }
    $newWorkDirectory = {
        $createWorkDirectory = { param([string]$Path); if (Test-Path -LiteralPath $Path) { throw 'temporary staging directory collision' }; [void][IO.Directory]::CreateDirectory($Path) }
        $removeWorkDirectory = { param([string]$Path); if (Test-Path -LiteralPath $Path) { [IO.Directory]::Delete($Path, $false) } }
        return New-GoalRouterTrustedWorkDirectory -TempRoot ([IO.Path]::GetTempPath().TrimEnd('\', '/')) -ResolvePathPort $resolvePath -CreateDirectoryPort $createWorkDirectory -RemoveDirectoryPort $removeWorkDirectory
    }.GetNewClosure()
    $readText = { param([string]$Path); return ConvertFrom-GoalRouterStrictUtf8Bytes -Bytes ([IO.File]::ReadAllBytes($Path)) -Label 'lifecycle text file' }
    $writeText = { param([string]$Path, [string]$Content); [IO.File]::WriteAllText($Path, $Content, [Text.UTF8Encoding]::new($false)) }
    $getHash = { param([string]$Path); return (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant() }
    $getArchiveEntries = {
        param([string]$Path)
        Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop
        $archive = [IO.Compression.ZipFile]::OpenRead($Path)
        try {
            $result = @()
            foreach ($entry in $archive.Entries) { $result += [pscustomobject]@{ FullName = $entry.FullName; ExternalAttributes = $entry.ExternalAttributes; IsDirectory = $entry.FullName.EndsWith('/') -or $entry.FullName.EndsWith('\'); Length = [int64]$entry.Length; CompressedLength = [int64]$entry.CompressedLength } }
            return $result
        } finally { $archive.Dispose() }
    }
    $extractArchive = {
        param([string]$ArchivePath, [string]$Destination)
        Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop
        [void][IO.Directory]::CreateDirectory($Destination)
        $archive = [IO.Compression.ZipFile]::OpenRead($ArchivePath)
        try {
            [int64]$totalWritten = 0
            foreach ($entry in $archive.Entries) {
                if ([int64]$entry.Length -gt $script:GoalRouterZipEntryLimit) { throw 'ZIP extraction entry size exceeds the trusted limit' }
                $target = Join-Path $Destination $entry.FullName
                $input = $entry.Open()
                try {
                    $output = [IO.File]::Open($target, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
                    try {
                        $buffer = New-Object byte[] 65536
                        [int64]$entryWritten = 0
                        while (($read = $input.Read($buffer, 0, $buffer.Length)) -gt 0) {
                            $entryWritten += $read; $totalWritten += $read
                            if ($entryWritten -gt $script:GoalRouterZipEntryLimit -or $totalWritten -gt $script:GoalRouterZipTotalLimit) { throw 'ZIP extraction content exceeds the trusted limit' }
                            $output.Write($buffer, 0, $read)
                        }
                        if ($entryWritten -ne [int64]$entry.Length) { throw 'ZIP extraction content length does not match metadata' }
                    } finally { $output.Dispose() }
                } finally { $input.Dispose() }
            }
        } finally { $archive.Dispose() }
    }
    $snapshot = {
        param([string]$Path)
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
            $sddl = $acl.GetSecurityDescriptorSddlForm([Security.AccessControl.AccessControlSections]::All)
            $bytes = [IO.File]::ReadAllBytes($Path)
            $content = $null; $textIsValid = $true
            try { $content = [Text.UTF8Encoding]::new($false, $true).GetString($bytes) }
            catch { $textIsValid = $false }
            return [pscustomobject]@{ Present = $true; Content = $content; TextIsValid = $textIsValid; Bytes = [Convert]::ToBase64String($bytes); Attributes = [int][IO.File]::GetAttributes($Path); SecurityDescriptorSddl = $sddl }
        }
        if (Test-Path -LiteralPath $Path) { throw "owned file target is not a regular file: $Path" }
        return [pscustomobject]@{ Present = $false; Content = $null; TextIsValid = $true; Bytes = $null; Attributes = $null; SecurityDescriptorSddl = $null }
    }
    $replace = {
        param([string]$Path, [string]$Content)
        $parent = Split-Path -Parent $Path
        $temporary = Join-Path $parent ('.goalrouter-' + [guid]::NewGuid().ToString('N') + '.tmp')
        $stream = [IO.File]::Open($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try {
            $bytes = [Text.UTF8Encoding]::new($false).GetBytes($Content)
            $stream.Write($bytes, 0, $bytes.Length)
        } finally { $stream.Dispose() }
        $hadTarget = Test-Path -LiteralPath $Path -PathType Leaf
        try {
            if (Test-Path -LiteralPath $Path) {
                if (-not $hadTarget -or (([IO.File]::GetAttributes($Path) -band [IO.FileAttributes]::ReparsePoint) -ne 0)) { throw "owned replacement target is unsafe: $Path" }
                [IO.File]::Replace($temporary, $Path, $null, $true)
            } else {
                Move-Item -LiteralPath $temporary -Destination $Path -ErrorAction Stop
            }
        } catch {
            $failure = $_
            if (Test-Path -LiteralPath $temporary -PathType Leaf) { Remove-Item -LiteralPath $temporary -ErrorAction Stop }
            throw $failure
        }
    }
    $restore = {
        param([string]$Path, $Snapshot)
        if (Test-Path -LiteralPath $Path -PathType Leaf) { Remove-Item -LiteralPath $Path -ErrorAction Stop }
        if ($Snapshot.Present) {
            [IO.File]::WriteAllBytes($Path, [Convert]::FromBase64String([string]$Snapshot.Bytes))
            [IO.File]::SetAttributes($Path, [IO.FileAttributes][int]$Snapshot.Attributes)
            $security = [Security.AccessControl.FileSecurity]::new()
            $security.SetSecurityDescriptorSddlForm([string]$Snapshot.SecurityDescriptorSddl, [Security.AccessControl.AccessControlSections]::All)
            Set-Acl -LiteralPath $Path -AclObject $security -ErrorAction Stop
        }
    }
    $ensureDirectory = {
        param([string]$Path)
        $testDirectory = { param([string]$Path); return Test-Path -LiteralPath $Path -PathType Container }
        $createDirectory = { param([string]$Path); [void][IO.Directory]::CreateDirectory($Path) }
        $getAttributes = { param([string]$Path); return [IO.File]::GetAttributes($Path) }
        $removeDirectory = { param([string]$Path); [IO.Directory]::Delete($Path, $false) }
        return @(Ensure-GoalRouterDirectoryChain -Path $Path -TestDirectory $testDirectory -CreateDirectory $createDirectory -GetAttributes $getAttributes -RemoveDirectory $removeDirectory)
    }
    $doctor = {
        param([string]$FilePath, [string[]]$Arguments)
        $output = @(& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $FilePath @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
        $nativeErrors = @($output | Where-Object { $_ -is [Management.Automation.ErrorRecord] })
        if ($exitCode -eq 0 -and $nativeErrors.Count -gt 0) { throw 'installed doctor emitted stderr despite a successful exit code' }
        foreach ($line in $output) { [Console]::Out.WriteLine($line) }
        return $exitCode
    }
    $removeFile = {
        param([string]$Path)
        if (Test-Path -LiteralPath $Path) {
            $resolved = @(Resolve-Path -LiteralPath $Path -ErrorAction Stop)
            if ($resolved.Count -ne 1 -or [string]$resolved[0].Provider.Name -cne 'FileSystem' -or -not (Test-GoalRouterWindowsPathEquivalent -First ([string]$resolved[0].ProviderPath) -Second $Path)) { throw "refusing file removal through provider redirection: $Path" }
            if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "refusing recursive removal of expected file target: $Path" }
            if (([IO.File]::GetAttributes($Path) -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "refusing file removal through reparse point: $Path" }
            Remove-Item -LiteralPath $Path -ErrorAction Stop
        }
    }
    $removeTree = {
        param([string]$Path)
        if (Test-Path -LiteralPath $Path) {
            if (([IO.File]::GetAttributes($Path) -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "refusing removal through reparse point: $Path" }
            if (Test-Path -LiteralPath $Path -PathType Container) {
                $nestedReparse = @(Get-ChildItem -LiteralPath $Path -Force -Recurse -ErrorAction Stop | Where-Object { ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 })
                if ($nestedReparse.Count -gt 0) { throw "refusing removal of tree containing a reparse point: $Path" }
            }
            Remove-Item -LiteralPath $Path -Recurse -ErrorAction Stop
        }
    }
    $getPathInfo = {
        param([string]$Path)
        $exists = Test-Path -LiteralPath $Path
        if (-not $exists) { return [pscustomobject]@{ Path = $Path; ProviderName = 'FileSystem'; ProviderPath = $Path; Exists = $false; IsContainer = $false; IsLeaf = $false; IsReparsePoint = $false; ContainsReparsePoint = $false; OwnerMatchesCurrentUser = $true; AclIsSafe = $true; Entries = @(); Sentinel = $null } }
        $resolved = @(Resolve-Path -LiteralPath $Path -ErrorAction Stop)
        if ($resolved.Count -ne 1) { throw "purge path must resolve exactly once: $Path" }
        $attributes = [IO.File]::GetAttributes($resolved[0].ProviderPath)
        $isContainer = Test-Path -LiteralPath $resolved[0].ProviderPath -PathType Container
        $isLeaf = Test-Path -LiteralPath $resolved[0].ProviderPath -PathType Leaf
        $sentinelPath = if ($isContainer) { Join-Path $resolved[0].ProviderPath $script:GoalRouterDirectorySentinel } else { $null }
        $sentinel = if ($isContainer -and (Test-Path -LiteralPath $sentinelPath -PathType Leaf)) { [IO.File]::ReadAllText($sentinelPath, [Text.Encoding]::UTF8) } else { $null }
        $entries = if ($isContainer) { @(Get-ChildItem -LiteralPath $resolved[0].ProviderPath -Force -ErrorAction Stop | ForEach-Object { $_.Name }) } else { @() }
        $containsReparse = $isContainer -and @(Get-ChildItem -LiteralPath $resolved[0].ProviderPath -Force -Recurse -ErrorAction Stop | Where-Object { ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 }).Count -gt 0
        $security = Get-GoalRouterWindowsPathSecurity -Path $resolved[0].ProviderPath
        return [pscustomobject]@{ Path = $Path; ProviderName = $resolved[0].Provider.Name; ProviderPath = $resolved[0].ProviderPath; Exists = $true; IsContainer = $isContainer; IsLeaf = $isLeaf; IsReparsePoint = (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0); ContainsReparsePoint = $containsReparse; OwnerMatchesCurrentUser = [bool]$security.OwnerMatchesCurrentUser; AclIsSafe = [bool]$security.AclIsSafe; Entries = $entries; Sentinel = $sentinel }
    }
    return [pscustomobject]@{
        GetHost = $getHost; ResolvePath = $resolvePath; ResolveLatestVersion = $resolveLatestVersion; NewWorkDirectory = $newWorkDirectory
        Native = $native; Download = $download; ReadText = $readText; WriteText = $writeText; GetHash = $getHash
        GetArchiveEntries = $getArchiveEntries; ExtractArchive = $extractArchive
        Snapshot = $snapshot; Replace = $replace; Restore = $restore; EnsureDirectory = $ensureDirectory; RemoveFile = $removeFile; RemoveTree = $removeTree
        GetUserPath = $getUserPath; SetUserPath = $setUserPath; Doctor = $doctor; GetPathInfo = $getPathInfo
    }
}

function Invoke-GoalRouterInstallCommit {
    param([Parameter(Mandatory = $true)]$Plan, [Parameter(Mandatory = $true)]$Ports)
    $snapshotPort = $Ports.Snapshot
    $replacePort = $Ports.Replace
    $restorePort = $Ports.Restore
    $getPathPort = $Ports.GetUserPath
    $setPathPort = $Ports.SetUserPath
    $doctorPort = $Ports.Doctor
    $snapshots = [ordered]@{}
    $paths = @($Plan.Replacements.Keys)
    $priorPath = & $getPathPort
    $applied = [System.Collections.ArrayList]::new()
    $createdDirectories = [System.Collections.ArrayList]::new()
    $directories = @()
    if ($Plan.PSObject.Properties.Name -contains 'Directories') { $directories = @($Plan.Directories) }
    $ensurePort = if ($directories.Count -gt 0) { $Ports.EnsureDirectory } else { $null }
    $removePort = if ($directories.Count -gt 0) { $Ports.RemoveTree } else { $null }
    $pathWasChanged = $false
    try {
        foreach ($directory in $directories) {
            foreach ($createdDirectory in @(& $ensurePort -Path ([string]$directory))) { [void]$createdDirectories.Add([string]$createdDirectory) }
        }
        foreach ($path in $paths) { $snapshots[$path] = & $snapshotPort -Path $path }
        foreach ($path in $paths) {
            & $replacePort -Path $path -Content ([string]$Plan.Replacements[$path])
            [void]$applied.Add($path)
        }
        if ([bool]$Plan.PathChange.Changed) {
            $pathWasChanged = $true
            & $setPathPort -Snapshot $Plan.PathChange.Snapshot
        }
        if (-not [bool]$Plan.SkipDoctor) {
            $doctorExitCode = & $doctorPort -FilePath ([string]$Plan.Doctor.FilePath) -Arguments @($Plan.Doctor.Arguments)
            if ([int]$doctorExitCode -ne 0) { throw "installed doctor failed with exit code $doctorExitCode" }
        }
    } catch {
        $failure = $_
        $rollbackFailures = [System.Collections.ArrayList]::new()
        if ($pathWasChanged) {
            try { & $setPathPort -Snapshot $priorPath }
            catch { [void]$rollbackFailures.Add($_.Exception.Message) }
        }
        for ($index = $applied.Count - 1; $index -ge 0; $index--) {
            $path = [string]$applied[$index]
            try { & $restorePort -Path $path -Snapshot $snapshots[$path] }
            catch { [void]$rollbackFailures.Add($_.Exception.Message) }
        }
        for ($index = $createdDirectories.Count - 1; $index -ge 0; $index--) {
            try { & $removePort -Path ([string]$createdDirectories[$index]) }
            catch { [void]$rollbackFailures.Add($_.Exception.Message) }
        }
        if ($rollbackFailures.Count -gt 0) { throw "$($failure.Exception.Message); rollback failures: $($rollbackFailures -join '; ')" }
        throw $failure
    }
}

function Assert-GoalRouterPowerShellVersion {
    param([Parameter(Mandatory = $true)][string]$Version)
    $parts = @(ConvertTo-GoalRouterVersionParts -Version $Version -Label 'PowerShell')
    if ($parts[0] -lt 5 -or ($parts[0] -eq 5 -and $parts[1] -lt 1)) { throw 'Windows PowerShell 5.1 or newer is required' }
}

function Assert-GoalRouterWindowsRuntime {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'install.ps1 requires Windows' }
    Assert-GoalRouterPowerShellVersion -Version $PSVersionTable.PSVersion.ToString()
}

function Assert-GoalRouterLifecyclePathInfo {
    param([Parameter(Mandatory = $true)]$Info, [Parameter(Mandatory = $true)][string]$Label, [bool]$AllowMissing, [string[]]$ProtectedRoots, [ValidateSet('Directory', 'File')][string]$RequiredKind = 'Directory')
    $path = [string]$Info.Path
    if (-not (Test-GoalRouterLifecyclePathText $path) -or $path.StartsWith('\\') -or $path -cnotmatch '\A[A-Za-z]:[\\/]') { throw "$Label must be an absolute local FileSystem path" }
    if ($path -match '(?:\A|[\\/])\.\.?(?:[\\/]|\z)') { throw "$Label must not contain dot path segments" }
    $canonicalPath = $path.Replace('/', '\')
    if ($canonicalPath.Substring(2).Contains('\\') -or $canonicalPath.Substring(2).Contains(':') -or $canonicalPath -match '[. ](?:\\|\z)') { throw "$Label contains an ambiguous Windows path component" }
    foreach ($component in @($canonicalPath.Substring(3).Split('\'))) {
        if ([string]::IsNullOrEmpty($component) -or $component -match '[<>:"|?*]' -or $component -cmatch '\A(?i:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM(?:[1-9]|[¹²³])|LPT(?:[1-9]|[¹²³]))(?:\..*)?\z') { throw "$Label contains an invalid or reserved Windows path component" }
    }
    if ([string]$Info.ProviderName -cne 'FileSystem' -or -not (Test-GoalRouterWindowsPathEquivalent -First ([string]$Info.ProviderPath) -Second $path)) { throw "$Label must resolve through the FileSystem provider without redirection" }
    $trimmed = $path.TrimEnd('\', '/')
    if ($trimmed -cmatch '\A[A-Za-z]:\z') { throw "$Label cannot be a drive root" }
    foreach ($root in $ProtectedRoots) {
        $protected = $root.TrimEnd('\', '/')
        if (Test-GoalRouterWindowsPathContainsOrEqual -Parent $trimmed -Child $protected) { throw "$Label cannot equal or contain a protected profile or AppData root" }
    }
    if ([bool]$Info.IsReparsePoint -or [bool]$Info.ParentIsReparsePoint) { throw "$Label cannot contain a reparse point" }
    if ($Info.PSObject.Properties.Name -contains 'AncestorChainIsSafe' -and -not [bool]$Info.AncestorChainIsSafe) { throw "$Label existing ancestor chain is unsafe" }
    if (-not [bool]$Info.Exists -and -not $AllowMissing) { throw "$Label does not exist" }
    if (-not [bool]$Info.Exists -and $Info.PSObject.Properties.Name -contains 'AncestorOwnerMatchesCurrentUser' -and -not [bool]$Info.AncestorOwnerMatchesCurrentUser) { throw "$Label nearest existing ancestor is not owned by the current user" }
    if (-not [bool]$Info.Exists -and $Info.PSObject.Properties.Name -contains 'AncestorAclIsSafe' -and -not [bool]$Info.AncestorAclIsSafe) { throw "$Label nearest existing ancestor ACL is unsafe" }
    if ([bool]$Info.Exists -and $Info.PSObject.Properties.Name -contains 'OwnerMatchesCurrentUser' -and -not [bool]$Info.OwnerMatchesCurrentUser) { throw "$Label is not owned by the current user" }
    if ([bool]$Info.Exists -and $Info.PSObject.Properties.Name -contains 'AclIsSafe' -and -not [bool]$Info.AclIsSafe) { throw "$Label ACL is unsafe" }
    if ([bool]$Info.Exists -and $RequiredKind -ceq 'Directory' -and -not [bool]$Info.IsContainer) { throw "$Label must be a directory" }
    if ([bool]$Info.Exists -and $RequiredKind -ceq 'File' -and -not [bool]$Info.IsLeaf) { throw "$Label must be a regular file" }
}

function Assert-GoalRouterHostRoot {
    param([Parameter(Mandatory = $true)]$Info, [Parameter(Mandatory = $true)][string]$Label)
    Assert-GoalRouterLifecyclePathInfo -Info $Info -Label $Label -AllowMissing $false -ProtectedRoots @() -RequiredKind 'Directory'
}

function New-GoalRouterTrustedWorkDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$TempRoot,
        [Parameter(Mandatory = $true)][scriptblock]$ResolvePathPort,
        [Parameter(Mandatory = $true)][scriptblock]$CreateDirectoryPort,
        [Parameter(Mandatory = $true)][scriptblock]$RemoveDirectoryPort,
        [scriptblock]$NewNamePort
    )
    if ($null -eq $NewNamePort) { $NewNamePort = { 'goalrouter-install-' + [guid]::NewGuid().ToString('N') } }
    $rootInfo = & $ResolvePathPort -Path $TempRoot -Kind 'Directory' -AllowMissing $false
    Assert-GoalRouterHostRoot -Info $rootInfo -Label 'temporary staging root'
    $path = Join-GoalRouterWindowsPath $TempRoot (& $NewNamePort)
    try {
        & $CreateDirectoryPort -Path $path
        $workInfo = & $ResolvePathPort -Path $path -Kind 'Directory' -AllowMissing $false
        Assert-GoalRouterHostRoot -Info $workInfo -Label 'temporary staging directory'
        return $path
    } catch {
        $failure = $_
        try { & $RemoveDirectoryPort -Path $path }
        catch { throw "$($failure.Exception.Message); trusted staging cleanup failed" }
        throw $failure
    }
}

function Assert-GoalRouterInstallDestination {
    param([Parameter(Mandatory = $true)]$Info, [Parameter(Mandatory = $true)][string]$Label, [string[]]$AllowedEntries)
    if (-not [bool]$Info.Exists) { return }
    if (-not [bool]$Info.IsContainer) { throw "$Label is not a directory" }
    if ($Info.PSObject.Properties.Name -contains 'OwnerMatchesCurrentUser' -and -not [bool]$Info.OwnerMatchesCurrentUser) { throw "$Label is not owned by the current user" }
    if ($Info.PSObject.Properties.Name -contains 'AclIsSafe' -and -not [bool]$Info.AclIsSafe) { throw "$Label grants unsafe write access" }
    if ($Info.PSObject.Properties.Name -contains 'ContainsReparsePoint' -and [bool]$Info.ContainsReparsePoint) { throw "$Label contains a recursive reparse point" }
    foreach ($entry in @($Info.Entries)) {
        if ([string]$entry -cnotin $AllowedEntries) { throw "$Label contains foreign or non-owned content: $entry" }
    }
}

function Get-GoalRouterWslVersion {
    param([Parameter(Mandatory = $true)][string]$Distribution, [Parameter(Mandatory = $true)][scriptblock]$NativeInvoker)
    $result = Invoke-GoalRouterLifecycleNative -NativeInvoker $NativeInvoker -Distribution $Distribution -Arguments @('wslinfo', '--wsl-version')
    $lines = @($result.Output)
    if ($lines.Count -ne 1) { throw 'selected WSL version output is invalid' }
    $match = [regex]::Match([string]$lines[0], '(?i)\A(?:WSL version:\s*)?([0-9]+(?:\.[0-9]+){0,3})\z')
    if (-not $match.Success) { throw 'selected WSL version output is invalid' }
    return $match.Groups[1].Value
}

function ConvertFrom-GoalRouterDockerVersionOutput {
    param([Parameter(Mandatory = $true)][object[]]$Output)
    if ($Output.Count -ne 1) { throw 'Docker client and daemon version output is invalid' }
    $match = [regex]::Match([string]$Output[0], '\A([0-9]+(?:\.[0-9]+){1,3}) ([0-9]+(?:\.[0-9]+){1,3})\z')
    if (-not $match.Success) { throw 'Docker client and daemon version output is invalid' }
    return @($match.Groups[1].Value, $match.Groups[2].Value)
}

function ConvertFrom-GoalRouterDockerArchitectureOutput {
    param([Parameter(Mandatory = $true)][object[]]$Output)
    if ($Output.Count -ne 1 -or [string]$Output[0] -cnotmatch '\A(?:x86_64|amd64|aarch64|arm64)\z') { throw 'Docker architecture output is invalid' }
    return [string]$Output[0]
}

function Convert-GoalRouterLifecyclePathToWsl {
    param([string]$Path, [string]$Distribution, [scriptblock]$NativeInvoker)
    $result = Invoke-GoalRouterLifecycleNative -NativeInvoker $NativeInvoker -Distribution $Distribution -Arguments @('wslpath', '-a', '-u', '--', $Path)
    $output = @($result.Output)
    if ($output.Count -ne 1 -or -not (Test-GoalRouterLifecyclePathText ([string]$output[0]))) { throw 'wslpath returned invalid output' }
    return [string]$output[0]
}

function Invoke-GoalRouterWindowsInstall {
    param([Parameter(Mandatory = $true)]$Options, [Parameter(Mandatory = $true)]$Ports)
    $getHostPort = $Ports.GetHost
    $resolvePort = $Ports.ResolvePath
    $resolveLatestVersionPort = if ($Ports.PSObject.Properties.Name -contains 'ResolveLatestVersion') { $Ports.ResolveLatestVersion } else { $null }
    $newWorkPort = $Ports.NewWorkDirectory
    $downloadPort = $Ports.Download
    $readPort = $Ports.ReadText
    $writePort = $Ports.WriteText
    $hashPort = $Ports.GetHash
    $entriesPort = $Ports.GetArchiveEntries
    $extractPort = $Ports.ExtractArchive
    $nativePort = $Ports.Native
    $getPathPort = $Ports.GetUserPath
    $ensurePort = $Ports.EnsureDirectory
    $snapshotPort = $Ports.Snapshot
    $removePort = $Ports.RemoveTree
    $hostInfo = & $getHostPort
    $selectedAuthMode = if ($Options.PSObject.Properties.Name -contains 'AuthMode') { [string]$Options.AuthMode } else { 'existing-session' }
    if ($selectedAuthMode -cnotin @('existing-session', 'api-key')) { throw 'invalid installer authentication mode' }
    Assert-GoalRouterPowerShellVersion -Version ([string]$hostInfo.PowerShellVersion)
    foreach ($hostRoot in @(
        @{ Path = [string]$hostInfo.UserProfile; Label = 'user profile root' },
        @{ Path = [string]$hostInfo.AppData; Label = 'roaming AppData root' },
        @{ Path = [string]$hostInfo.LocalAppData; Label = 'local AppData root' }
    )) {
        $hostRootInfo = & $resolvePort -Path ([string]$hostRoot.Path) -Kind 'Directory' -AllowMissing $false
        Assert-GoalRouterHostRoot -Info $hostRootInfo -Label ([string]$hostRoot.Label)
    }
    $layout = Get-GoalRouterWindowsLayout -LocalAppData $hostInfo.LocalAppData -AppData $hostInfo.AppData -UserProfile $hostInfo.UserProfile -InstallRoot $Options.InstallRoot -BinDir $Options.BinDir -ConfigFile $Options.ConfigFile -StateDir $Options.StateDir -CodexHome $Options.CodexHome
    $expectedBinDir = Join-GoalRouterWindowsPath $layout.InstallRoot 'bin'
    if (-not (Test-GoalRouterWindowsPathEquivalent -First $layout.BinDir -Second $expectedBinDir)) { throw 'bin directory must be the install root bin child so trusted control is physically discoverable' }
    $protectedRoots = @([string]$hostInfo.UserProfile, [string]$hostInfo.AppData, [string]$hostInfo.LocalAppData)
    foreach ($destination in @(
        @{ Path = $layout.InstallRoot; Label = 'install root'; Missing = $true },
        @{ Path = $layout.BinDir; Label = 'bin directory'; Missing = $true },
        @{ Path = $layout.ConfigDir; Label = 'config directory'; Missing = $true },
        @{ Path = $layout.ConfigFile; Label = 'config file'; Missing = $true; Kind = 'File' },
        @{ Path = $layout.StateDir; Label = 'state directory'; Missing = $true },
        @{ Path = $layout.CodexHome; Label = 'Codex home'; Missing = $false }
    )) {
        $requiredKind = if ($destination.ContainsKey('Kind')) { [string]$destination.Kind } else { 'Directory' }
        $info = & $resolvePort -Path $destination.Path -Kind $requiredKind -AllowMissing $destination.Missing
        Assert-GoalRouterLifecyclePathInfo -Info $info -Label $destination.Label -AllowMissing $destination.Missing -ProtectedRoots $protectedRoots -RequiredKind $requiredKind
    }
    foreach ($pair in @(@($layout.BinDir, $layout.ConfigDir), @($layout.BinDir, $layout.StateDir), @($layout.ConfigDir, $layout.StateDir))) {
        $left = $pair[0].TrimEnd('\', '/'); $right = $pair[1].TrimEnd('\', '/')
        if ((Test-GoalRouterWindowsPathContainsOrEqual -Parent $left -Child $right) -or (Test-GoalRouterWindowsPathContainsOrEqual -Parent $right -Child $left)) { throw 'owned installation destinations overlap' }
    }
    $installRootTrimmed = $layout.InstallRoot.TrimEnd('\', '/')
    $configTrimmed = $layout.ConfigDir.TrimEnd('\', '/')
    if ((Test-GoalRouterWindowsPathContainsOrEqual -Parent $configTrimmed -Child $installRootTrimmed) -or (Test-GoalRouterWindowsPathContainsOrEqual -Parent $installRootTrimmed -Child $configTrimmed)) { throw 'configuration and install root destinations overlap' }
    $stateTrimmed = $layout.StateDir.TrimEnd('\', '/')
    $expectedState = Join-GoalRouterWindowsPath $layout.InstallRoot 'state'
    if (((Test-GoalRouterWindowsPathContainsOrEqual -Parent $stateTrimmed -Child $installRootTrimmed) -or (Test-GoalRouterWindowsPathContainsOrEqual -Parent $installRootTrimmed -Child $stateTrimmed)) -and -not (Test-GoalRouterWindowsPathEquivalent -First $layout.StateDir -Second $expectedState)) { throw 'custom state and install root destinations overlap' }
    foreach ($ownedPath in @($layout.InstallRoot, $layout.BinDir, $layout.ConfigDir, $layout.StateDir)) {
        $codexTrimmed = $layout.CodexHome.TrimEnd('\', '/')
        $ownedTrimmed = $ownedPath.TrimEnd('\', '/')
        if ((Test-GoalRouterWindowsPathContainsOrEqual -Parent $codexTrimmed -Child $ownedTrimmed) -or (Test-GoalRouterWindowsPathContainsOrEqual -Parent $ownedTrimmed -Child $codexTrimmed)) { throw 'Codex home overlaps an owned installation destination' }
    }
    $recoverySnapshot = & $snapshotPort -Path $layout.RecoveryPath
    if ($recoverySnapshot.Present) { throw 'an uninstall recovery is active; retry the physical uninstaller' }
    if (-not [bool]$Options.Yes) { throw 'installation requires -Yes' }
    if ([string]$Options.Version -ceq 'latest') {
        if (-not [string]::IsNullOrEmpty([string]$Options.ReleaseBase)) { throw '-Version latest is unsupported with a custom release base' }
        if ($null -eq $resolveLatestVersionPort) { throw 'latest stable release resolver is unavailable' }
        $resolvedVersion = [string](& $resolveLatestVersionPort)
        if ($resolvedVersion -cnotmatch '\A[0-9]+\.[0-9]+\.[0-9]+\z') { throw 'latest stable release version is invalid' }
        if (-not [string]::IsNullOrEmpty([string]$Options.Image)) {
            $latestImage = [string]$Options.Image
            if ($latestImage.LastIndexOf(':') -le $latestImage.LastIndexOf('/') -or -not $latestImage.EndsWith(':latest', [StringComparison]::Ordinal)) { throw '-Version latest requires an image tagged latest' }
            $Options.Image = (Get-GoalRouterImageRepository $latestImage) + ':' + $resolvedVersion
        }
        $Options.Version = $resolvedVersion
    }
    $imageValue = if ([string]::IsNullOrEmpty($Options.Image)) { "ghcr.io/vparla/goalrouter:$($Options.Version)" } else { [string]$Options.Image }
    if ($imageValue -notmatch '\A(?:[a-z0-9]+(?:(?:[.]|__|_|-+)[a-z0-9]+)*/)*(?:[a-z0-9]+(?:(?:[.]|__|_|-+)[a-z0-9]+)*)(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?\z' -and $imageValue -notmatch '\A(?:[A-Za-z0-9.-]+(?::[0-9]+)?/)+(?:[a-z0-9]+(?:(?:[.]|__|_|-+)[a-z0-9]+)*)(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?\z') { throw 'requested image reference is invalid' }
    $existingManifestSnapshot = & $snapshotPort -Path $layout.ManifestPath
    $existingChecksumPath = Join-GoalRouterWindowsPath $layout.InstallRoot 'install.sha256'
    $existingChecksumSnapshot = & $snapshotPort -Path $existingChecksumPath
    $existingManifest = $null
    if ($existingManifestSnapshot.Present -or $existingChecksumSnapshot.Present) {
        $manifestTextIsValid = -not ($existingManifestSnapshot.PSObject.Properties.Name -contains 'TextIsValid') -or [bool]$existingManifestSnapshot.TextIsValid
        $checksumTextIsValid = -not ($existingChecksumSnapshot.PSObject.Properties.Name -contains 'TextIsValid') -or [bool]$existingChecksumSnapshot.TextIsValid
        $existingIsValid = $existingManifestSnapshot.Present -and $existingChecksumSnapshot.Present -and $manifestTextIsValid -and $checksumTextIsValid -and ([string]$existingChecksumSnapshot.Content).Trim() -cmatch '\A[0-9a-f]{64}\z' -and (Get-GoalRouterStringSha256 ([string]$existingManifestSnapshot.Content)) -ceq ([string]$existingChecksumSnapshot.Content).Trim()
        if ($existingIsValid) {
            $priorValidationErrorCount = $Error.Count
            try {
                $existingManifest = [string]$existingManifestSnapshot.Content | ConvertFrom-Json -ErrorAction Stop
                Assert-GoalRouterExistingInstallManifest -Manifest $existingManifest -Json ([string]$existingManifestSnapshot.Content) -Layout $layout
            } catch {
                $existingIsValid = $false
                $existingManifest = $null
                $addedValidationErrorCount = $Error.Count - $priorValidationErrorCount
                for ($validationErrorIndex = 0; $validationErrorIndex -lt $addedValidationErrorCount; $validationErrorIndex++) { $Error.RemoveAt(0) }
            }
        }
        if (-not $existingIsValid -and -not [bool]$Options.Force) { throw 'existing install control is corrupt; use -Force for explicit repair' }
        if ($null -ne $existingManifest -and (-not (Test-GoalRouterWindowsPathEquivalent -First ([string]$existingManifest.owned.install_root) -Second $layout.InstallRoot) -or -not (Test-GoalRouterWindowsPathEquivalent -First ([string]$existingManifest.owned.bin_dir) -Second $layout.BinDir))) { throw 'existing install control belongs to different destinations' }
        if ($existingIsValid) {
            $stateManifestSnapshot = & $snapshotPort -Path (Join-GoalRouterWindowsPath $layout.StateDir 'install.json')
            $stateChecksumSnapshot = & $snapshotPort -Path (Join-GoalRouterWindowsPath $layout.StateDir 'install.sha256')
            $stateParityIsValid = $stateManifestSnapshot.Present -and $stateChecksumSnapshot.Present -and [string]$stateManifestSnapshot.Content -ceq [string]$existingManifestSnapshot.Content -and [string]$stateChecksumSnapshot.Content -ceq [string]$existingChecksumSnapshot.Content
            if (-not $stateParityIsValid -and -not [bool]$Options.Force) { throw 'existing runtime state control parity is missing or corrupt; use -Force for explicit repair' }
        }
    }

    $destinationChecks = @(
        @{ Path = $layout.InstallRoot; Label = 'install root'; Allowed = @('bin', 'state', 'install.json', 'install.sha256', $script:GoalRouterRecoveryName, $script:GoalRouterDirectorySentinel) },
        @{ Path = $layout.BinDir; Label = 'bin directory'; Allowed = @('goalrouter.ps1', 'goalrouter.cmd', 'install.ps1', 'uninstall.ps1') },
        @{ Path = $layout.ConfigDir; Label = 'config directory'; Allowed = @($script:GoalRouterDirectorySentinel, (Split-Path -Leaf $layout.ConfigFile)) },
        @{ Path = $layout.StateDir; Label = 'state directory'; Allowed = @($script:GoalRouterDirectorySentinel, 'install.json', 'install.sha256', 'runs', 'reports') }
    )
    foreach ($destination in $destinationChecks) {
        $destinationInfo = & $Ports.GetPathInfo -Path ([string]$destination.Path)
        Assert-GoalRouterInstallDestination -Info $destinationInfo -Label ([string]$destination.Label) -AllowedEntries @($destination.Allowed)
        if ($destinationInfo.Exists -and $null -eq $existingManifest -and -not [bool]$Options.Force -and @($destinationInfo.Entries).Count -gt 0 -and [string]$destinationInfo.Sentinel -cne $script:GoalRouterDirectorySentinelValue) {
            $rootEntries = @($destinationInfo.Entries)
            $boundedEmptyUninstallShell = $destination.Label -ceq 'install root' -and $rootEntries.Count -eq 1 -and [string]$rootEntries[0] -ieq 'bin'
            if ($boundedEmptyUninstallShell) {
                $residualBin = & $Ports.GetPathInfo -Path $layout.BinDir
                $boundedEmptyUninstallShell = $residualBin.Exists -and $residualBin.IsContainer -and -not $residualBin.IsReparsePoint -and @($residualBin.Entries).Count -eq 0
            }
            if (-not $boundedEmptyUninstallShell) { throw "$($destination.Label) is nonempty and lacks trusted GoalRouter ownership" }
        }
    }

    $kernel = Invoke-GoalRouterLifecycleNative -NativeInvoker $nativePort -Distribution $Options.WslDistribution -Arguments @('uname', '-r')
    if (@($kernel.Output).Count -ne 1 -or [string]$kernel.Output[0] -notmatch 'WSL2') { throw 'selected WSL distribution is not ready under WSL2' }
    $wslVersion = Get-GoalRouterWslVersion -Distribution $Options.WslDistribution -NativeInvoker $nativePort
    $dockerVersionsResult = Invoke-GoalRouterLifecycleNative -NativeInvoker $nativePort -Distribution $Options.WslDistribution -Arguments @('docker', 'version', '--format', '{{.Client.Version}} {{.Server.Version}}')
    $dockerVersionFields = @(ConvertFrom-GoalRouterDockerVersionOutput -Output @($dockerVersionsResult.Output))
    $architectureResult = Invoke-GoalRouterLifecycleNative -NativeInvoker $nativePort -Distribution $Options.WslDistribution -Arguments @('docker', 'info', '--format', '{{.Architecture}}')
    $dockerArchitecture = ConvertFrom-GoalRouterDockerArchitectureOutput -Output @($architectureResult.Output)
    $expectedDockerArchitecture = if ($hostInfo.Platform -ceq 'linux/amd64') { @('x86_64', 'amd64') } else { @('aarch64', 'arm64') }
    if ($dockerArchitecture -cnotin $expectedDockerArchitecture) { throw 'Docker architecture does not match the supported Windows host architecture' }

    $releaseBaseValue = if ([string]::IsNullOrEmpty($Options.ReleaseBase)) { "https://github.com/vparla/GoalRouter/releases/download/v$($Options.Version)" } else { [string]$Options.ReleaseBase }
    [void](Assert-GoalRouterReleaseUri -Uri $releaseBaseValue -AllowLoopbackHttp ([bool]$Options.AllowLoopbackHttp))
    $workDirectory = & $newWorkPort
    try {
        $checksumsPath = Join-GoalRouterWindowsPath $workDirectory 'SHA256SUMS'
        $releaseManifestPath = Join-GoalRouterWindowsPath $workDirectory $script:GoalRouterReleaseManifestName
        $archiveName = "goalrouter-$($Options.Version)-windows.zip"
        $archivePath = Join-GoalRouterWindowsPath $workDirectory $archiveName
        & $downloadPort -Uri "$releaseBaseValue/SHA256SUMS" -Destination $checksumsPath -AllowLoopbackHttp ([bool]$Options.AllowLoopbackHttp)
        & $downloadPort -Uri "$releaseBaseValue/$($script:GoalRouterReleaseManifestName)" -Destination $releaseManifestPath -AllowLoopbackHttp ([bool]$Options.AllowLoopbackHttp)
        & $downloadPort -Uri "$releaseBaseValue/$archiveName" -Destination $archivePath -AllowLoopbackHttp ([bool]$Options.AllowLoopbackHttp)
        $checksumText = & $readPort -Path $checksumsPath
        $expectedManifestHash = Get-GoalRouterChecksum -Text $checksumText -AssetName $script:GoalRouterReleaseManifestName
        $expectedArchiveHash = Get-GoalRouterChecksum -Text $checksumText -AssetName $archiveName
        if ((& $hashPort -Path $releaseManifestPath) -cne $expectedManifestHash) { throw 'downloaded release manifest checksum mismatch' }
        if ((& $hashPort -Path $archivePath) -cne $expectedArchiveHash) { throw 'downloaded Windows archive checksum mismatch' }
        $releaseJson = & $readPort -Path $releaseManifestPath
        $releaseManifest = Assert-GoalRouterReleaseManifest -Json $releaseJson -RequestedVersion $Options.Version -RequestedImage $imageValue -Platform $hostInfo.Platform -WindowsVersion $hostInfo.WindowsVersion -PowerShellVersion $hostInfo.PowerShellVersion -WslVersion $wslVersion -DockerClientVersion $dockerVersionFields[0] -DockerServerVersion $dockerVersionFields[1]
        $archiveEntries = @(& $entriesPort -Path $archivePath)
        Assert-GoalRouterZipEntries -Entries $archiveEntries
        $extractDirectory = Join-GoalRouterWindowsPath $workDirectory 'extract'
        & $extractPort -ArchivePath $archivePath -Destination $extractDirectory
        $candidate = Test-GoalRouterCandidateImage -Manifest $releaseManifest -Distribution $Options.WslDistribution -Platform $hostInfo.Platform -NativeInvoker $nativePort
        $templateResult = Invoke-GoalRouterLifecycleNative -NativeInvoker $nativePort -Distribution $Options.WslDistribution -Arguments @('docker', 'run', '--rm', '--read-only', '--tmpfs', '/tmp:rw,exec,nosuid,size=64m,mode=1777', $candidate.RepoDigest, 'config', 'template')
        $templatePath = Join-GoalRouterWindowsPath $workDirectory 'task-models.yaml'
        $templateText = @($templateResult.Output) -join "`n"
        if ([string]::IsNullOrEmpty($templateText)) { throw 'candidate configuration template is empty' }
        & $writePort -Path $templatePath -Content ($templateText + "`n")
        $templateWsl = Convert-GoalRouterLifecyclePathToWsl -Path $templatePath -Distribution $Options.WslDistribution -NativeInvoker $nativePort
        [void](Invoke-GoalRouterLifecycleNative -NativeInvoker $nativePort -Distribution $Options.WslDistribution -Arguments @('docker', 'run', '--rm', '--read-only', '--tmpfs', '/tmp:rw,exec,nosuid,size=64m,mode=1777', '--mount', "type=bind,src=$templateWsl,dst=/candidate.yaml,readonly", '--env', 'GOALROUTER_CONFIG=/candidate.yaml', $candidate.RepoDigest, 'config', 'validate'))
        $existingConfig = & $snapshotPort -Path $layout.ConfigFile
        if ($existingConfig.Present -and -not [bool]$Options.ResetConfig) {
            $existingConfigWsl = Convert-GoalRouterLifecyclePathToWsl -Path $layout.ConfigFile -Distribution $Options.WslDistribution -NativeInvoker $nativePort
            [void](Invoke-GoalRouterLifecycleNative -NativeInvoker $nativePort -Distribution $Options.WslDistribution -Arguments @('docker', 'run', '--rm', '--read-only', '--tmpfs', '/tmp:rw,exec,nosuid,size=64m,mode=1777', '--mount', "type=bind,src=$existingConfigWsl,dst=/candidate.yaml,readonly", '--env', 'GOALROUTER_CONFIG=/candidate.yaml', $candidate.RepoDigest, 'config', 'validate'))
        }

        $currentPath = & $getPathPort
        $pathChange = Add-GoalRouterUserPathEntry -Snapshot $currentPath -OwnedEntry $layout.BinDir -NoPathUpdate:([bool]$Options.NoPathUpdate)
        if ($null -ne $existingManifest -and [bool]$existingManifest.path_ownership.installer_added -and [string]$existingManifest.path_ownership.owned_value -ieq $layout.BinDir) {
            $pathChange = [pscustomobject]@{
                Changed = $false; InstallerAdded = $true; OwnedValue = [string]$existingManifest.path_ownership.owned_value
                UpdateEnabled = [bool]$existingManifest.path_ownership.update_enabled
                BeforeState = [string]$existingManifest.path_ownership.before_state
                BeforeValueKind = $existingManifest.path_ownership.before_value_kind
                AfterValueKind = $existingManifest.path_ownership.after_value_kind
                AfterSha256 = $existingManifest.path_ownership.after_sha256
                Before = [pscustomobject]@{ Present = $false; Value = $null; ValueKind = $null }
                Snapshot = Copy-GoalRouterPathSnapshot $currentPath
            }
        }
        $pathOwnership = [pscustomobject]@{ InstallerAdded = $pathChange.InstallerAdded; UpdateEnabled = if ($pathChange.PSObject.Properties.Name -contains 'UpdateEnabled') { [bool]$pathChange.UpdateEnabled } else { -not [bool]$Options.NoPathUpdate }; OwnedValue = $pathChange.OwnedValue; Before = $pathChange.Before; Snapshot = $pathChange.Snapshot; BeforeState = $pathChange.BeforeState; BeforeValueKind = $pathChange.BeforeValueKind; AfterValueKind = $pathChange.AfterValueKind; AfterSha256 = $pathChange.AfterSha256 }
        $installManifest = New-GoalRouterInstallManifest -Version $Options.Version -ImageReference (Get-GoalRouterImageRepository $imageValue) -ImageDigest $candidate.ImageDigest -ImagePlatform $hostInfo.Platform -SourceRevision $candidate.Revision -WslDistribution $Options.WslDistribution -Layout $layout -PathOwnership $pathOwnership -ReleaseBase $releaseBaseValue
        $installJson = ConvertTo-GoalRouterCanonicalInstallManifestJson $installManifest
        $installChecksum = Get-GoalRouterStringSha256 $installJson
        $replacements = [ordered]@{}
        foreach ($name in @('goalrouter.ps1', 'goalrouter.cmd', 'install.ps1', 'uninstall.ps1')) {
            $target = Join-GoalRouterWindowsPath $layout.BinDir $name
            $replacements[$target] = & $readPort -Path (Join-GoalRouterWindowsPath $extractDirectory $name)
        }
        $replacements[(Join-GoalRouterWindowsPath $layout.InstallRoot $script:GoalRouterDirectorySentinel)] = $script:GoalRouterDirectorySentinelValue
        $replacements[(Join-GoalRouterWindowsPath $layout.ConfigDir $script:GoalRouterDirectorySentinel)] = $script:GoalRouterDirectorySentinelValue
        $replacements[(Join-GoalRouterWindowsPath $layout.StateDir $script:GoalRouterDirectorySentinel)] = $script:GoalRouterDirectorySentinelValue
        if (-not $existingConfig.Present -or [bool]$Options.ResetConfig) { $replacements[$layout.ConfigFile] = $templateText + "`n" }
        $replacements[$layout.ManifestPath] = $installJson
        $replacements[$existingChecksumPath] = $installChecksum + "`n"
        $replacements[(Join-GoalRouterWindowsPath $layout.StateDir 'install.json')] = $installJson
        $replacements[(Join-GoalRouterWindowsPath $layout.StateDir 'install.sha256')] = $installChecksum + "`n"
        $doctorArguments = @('--auth-mode', $selectedAuthMode, '--config', $layout.ConfigFile, '--state-dir', $layout.StateDir, '--codex-home', $layout.CodexHome, 'doctor')
        if ([bool]$Options.SkipAccount) { $doctorArguments += '-SkipAccount' }
        $plan = [pscustomobject]@{ Directories = @($layout.InstallRoot, $layout.BinDir, $layout.ConfigDir, $layout.StateDir); Replacements = $replacements; PathChange = $pathChange; Doctor = [pscustomobject]@{ FilePath = $layout.LauncherPath; Arguments = $doctorArguments }; SkipDoctor = [bool]$Options.SkipDoctor }
        & $removePort -Path $workDirectory
        $workDirectory = $null
        Invoke-GoalRouterInstallCommit -Plan $plan -Ports $Ports
        [Console]::Out.WriteLine("GoalRouter $($Options.Version) installed at $($layout.LauncherPath)")
    } finally {
        if (-not [string]::IsNullOrEmpty($workDirectory)) { & $removePort -Path $workDirectory }
    }
}

$goalRouterInstallerIsDotSourced = $MyInvocation.InvocationName -ceq '.'
if ($goalRouterInstallerIsDotSourced) { return }

try {
    $options = [pscustomobject]@{
        Version = $Version; InstallRoot = $InstallRoot; BinDir = $BinDir; ConfigFile = $ConfigFile
        StateDir = $StateDir; CodexHome = $CodexHome; WslDistribution = $WslDistribution
        Yes = [bool]$Yes; Force = [bool]$Force; ResetConfig = [bool]$ResetConfig
        NoPathUpdate = [bool]$NoPathUpdate; SkipDoctor = [bool]$SkipDoctor; SkipAccount = [bool]$SkipAccount; AuthMode = $AuthMode
        ReleaseBase = $ReleaseBase; AllowLoopbackHttp = [bool]$AllowLoopbackHttp; Image = $Image
    }
    Invoke-GoalRouterWindowsInstall -Options $options -Ports (New-GoalRouterProductionLifecyclePorts)
} catch {
    [Console]::Error.WriteLine("goalrouter installer: $($_.Exception.Message)")
    exit 1
}
