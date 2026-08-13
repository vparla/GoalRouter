# SPDX-License-Identifier: MIT
# File: scripts/uninstall.ps1
# Purpose: Windows per-user uninstaller for GoalRouter
#requires -Version 5.1

[CmdletBinding()]
param(
    [string]$InstallRoot,
    [switch]$Purge,
    [switch]$Yes
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$WarningPreference = 'Stop'
$goalRouterUninstallerIsDotSourced = $MyInvocation.InvocationName -ceq '.'

function Assert-GoalRouterTrustedStateParity {
    param([Parameter(Mandatory = $true)][string]$TrustedJson, [AllowNull()][string]$StateJson)
    if ([string]::IsNullOrEmpty($StateJson)) { throw 'runtime state parity manifest is missing' }
    if ($TrustedJson -cne $StateJson) { throw 'runtime state parity does not match trusted install control' }
}

function Assert-GoalRouterBootstrapLeaf {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedPath,
        [Parameter(Mandatory = $true)]$Inspection
    )
    $normalize = { param([string]$Value); $Value.Replace('/', '\').TrimEnd('\') }
    if ([string]::IsNullOrEmpty($Path) -or (& $normalize $Path) -ine (& $normalize $ExpectedPath) -or (& $normalize $Path) -cnotmatch '\A[A-Za-z]:\\[^\\].*\z') { throw 'bootstrap lifecycle path is not the exact expected local path' }
    if ([int]$Inspection.Count -ne 1 -or [string]$Inspection.ProviderName -ine 'FileSystem' -or (& $normalize ([string]$Inspection.ProviderPath)) -ine (& $normalize $Path)) { throw 'bootstrap lifecycle path does not have exact FileSystem provider identity' }
    if (-not [bool]$Inspection.IsLeaf) { throw 'bootstrap lifecycle path is not a regular leaf' }
    if ([bool]$Inspection.IsReparsePoint) { throw 'bootstrap lifecycle path is a reparse point' }
    if (-not [bool]$Inspection.OwnerMatchesCurrentUser) { throw 'bootstrap lifecycle path is not owned by the current user' }
    if (-not [bool]$Inspection.AclIsSafe) { throw 'bootstrap lifecycle path ACL is unsafe' }
    if ($Inspection.PSObject.Properties.Name -notcontains 'AncestorChainIsSafe' -or -not [bool]$Inspection.AncestorChainIsSafe) { throw 'bootstrap lifecycle path ancestor chain is unsafe' }
    return [string]$Inspection.ProviderPath
}

function Get-GoalRouterBootstrapMutationRightsMask {
    return [long]([Security.AccessControl.FileSystemRights]::WriteData -bor [Security.AccessControl.FileSystemRights]::AppendData -bor [Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor [Security.AccessControl.FileSystemRights]::WriteAttributes -bor [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor [Security.AccessControl.FileSystemRights]::Delete -bor [Security.AccessControl.FileSystemRights]::ChangePermissions -bor [Security.AccessControl.FileSystemRights]::TakeOwnership)
}

function Test-GoalRouterBootstrapAclRightsUnsafe {
    param([Parameter(Mandatory = $true)][long]$Rights)
    return ($Rights -band (Get-GoalRouterBootstrapMutationRightsMask)) -ne 0
}

function Assert-GoalRouterBootstrapPhysicalLeaf {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$ExpectedPath)
    $resolved = @(Resolve-Path -LiteralPath $Path -ErrorAction Stop)
    $isLeaf = $resolved.Count -eq 1 -and (Test-Path -LiteralPath $Path -PathType Leaf)
    $isReparse = $isLeaf -and (([IO.File]::GetAttributes($Path) -band [IO.FileAttributes]::ReparsePoint) -ne 0)
    $ownerMatches = $false
    $aclIsSafe = $false
    $ancestorChainIsSafe = $true
    if ($isLeaf -and -not $isReparse) {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
        $ownerSid = if ([string]$acl.Owner -match '\AS-\d(?:-\d+)+\z') { [string]$acl.Owner } else { ([Security.Principal.NTAccount]$acl.Owner).Translate([Security.Principal.SecurityIdentifier]).Value }
        $allowedSids = @($identity.User.Value, 'S-1-5-18', 'S-1-5-32-544', 'S-1-3-0')
        $unsafe = @($acl.Access | Where-Object {
            $sid = $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
            $_.AccessControlType -ceq [Security.AccessControl.AccessControlType]::Allow -and (Test-GoalRouterBootstrapAclRightsUnsafe -Rights ([long]$_.FileSystemRights)) -and $sid -notin $allowedSids
        }).Count -gt 0
        $ownerMatches = $ownerSid -ceq $identity.User.Value
        $aclIsSafe = -not $unsafe
        $cursor = Split-Path -Parent ([string]$resolved[0].ProviderPath)
        while (-not [string]::IsNullOrEmpty($cursor)) {
            if ($cursor.Replace('/', '\').TrimEnd('\') -cmatch '\A[A-Za-z]:\z') { break }
            if (([IO.File]::GetAttributes($cursor) -band [IO.FileAttributes]::ReparsePoint) -ne 0) { $ancestorChainIsSafe = $false; break }
            $ancestorAcl = Get-Acl -LiteralPath $cursor -ErrorAction Stop
            $ancestorOwnerSid = if ([string]$ancestorAcl.Owner -match '\AS-\d(?:-\d+)+\z') { [string]$ancestorAcl.Owner } else { ([Security.Principal.NTAccount]$ancestorAcl.Owner).Translate([Security.Principal.SecurityIdentifier]).Value }
            $ancestorUnsafe = @($ancestorAcl.Access | Where-Object {
                $sid = $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
                $_.AccessControlType -ceq [Security.AccessControl.AccessControlType]::Allow -and (Test-GoalRouterBootstrapAclRightsUnsafe -Rights ([long]$_.FileSystemRights)) -and $sid -notin $allowedSids
            }).Count -gt 0
            if ($ancestorOwnerSid -notin $allowedSids -or $ancestorUnsafe) { $ancestorChainIsSafe = $false; break }
            $next = Split-Path -Parent $cursor
            if ([string]::IsNullOrEmpty($next) -or $next -ceq $cursor) { break }
            $cursor = $next
        }
    }
    $inspection = [pscustomobject]@{ Count = $resolved.Count; ProviderName = if ($resolved.Count -eq 1) { [string]$resolved[0].Provider.Name } else { $null }; ProviderPath = if ($resolved.Count -eq 1) { [string]$resolved[0].ProviderPath } else { $null }; IsLeaf = $isLeaf; IsReparsePoint = $isReparse; OwnerMatchesCurrentUser = $ownerMatches; AclIsSafe = $aclIsSafe; AncestorChainIsSafe = $ancestorChainIsSafe }
    return Assert-GoalRouterBootstrapLeaf -Path $Path -ExpectedPath $ExpectedPath -Inspection $inspection
}

$selectedInstallRootArgument = $InstallRoot
$selectedConfirmedArgument = [bool]$Yes
try {
    $installerLibrary = Join-Path $PSScriptRoot 'install.ps1'
    if (-not (Test-Path -LiteralPath $installerLibrary -PathType Leaf)) {
        if ($goalRouterUninstallerIsDotSourced) { throw 'trusted lifecycle library is missing' }
        $fallbackInstallRoot = if ([string]::IsNullOrEmpty($selectedInstallRootArgument)) { Join-Path $env:LOCALAPPDATA 'GoalRouter' } else { $selectedInstallRootArgument }
        $expectedUninstaller = Join-Path (Join-Path $fallbackInstallRoot 'bin') 'uninstall.ps1'
        $normalizeWindowsPath = { param([string]$Path); return $Path.Replace('/', '\').TrimEnd('\') }
        if ([string]::IsNullOrEmpty($fallbackInstallRoot) -or (& $normalizeWindowsPath $fallbackInstallRoot) -notmatch '\A[A-Za-z]:\\[^\\].*\z' -or (& $normalizeWindowsPath $PSCommandPath) -ine (& $normalizeWindowsPath $expectedUninstaller)) { throw 'trusted lifecycle library is missing' }
        foreach ($requiredAbsent in @('install.json', 'install.sha256', 'uninstall-recovery.json')) {
            if (Test-Path -LiteralPath (Join-Path $fallbackInstallRoot $requiredAbsent)) { throw 'trusted lifecycle library is missing before bounded final cleanup' }
        }
        foreach ($requiredAbsent in @('goalrouter.ps1', 'goalrouter.cmd', 'install.ps1')) {
            if (Test-Path -LiteralPath (Join-Path $PSScriptRoot $requiredAbsent)) { throw 'trusted lifecycle library is missing before bounded final cleanup' }
        }
        $resolvedSelfTarget = Assert-GoalRouterBootstrapPhysicalLeaf -Path $PSCommandPath -ExpectedPath $expectedUninstaller
        Remove-Item -LiteralPath $resolvedSelfTarget -ErrorAction Stop
        [Console]::Out.WriteLine('GoalRouter final uninstaller cleanup completed')
        exit 0
    }
    $resolvedInstallerLibrary = if ($goalRouterUninstallerIsDotSourced) { $installerLibrary } else { Assert-GoalRouterBootstrapPhysicalLeaf -Path $installerLibrary -ExpectedPath (Join-Path $PSScriptRoot 'install.ps1') }
    . $resolvedInstallerLibrary
    $InstallRoot = $selectedInstallRootArgument
    $Yes = $selectedConfirmedArgument
} catch {
    if ($goalRouterUninstallerIsDotSourced) { throw }
    [Console]::Error.WriteLine('goalrouter uninstaller: bootstrap trust validation failed')
    exit 1
}

function Assert-SafeGoalRouterRemoval {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$UserRoot,
        [Parameter(Mandatory = $true)][string]$AppData,
        [Parameter(Mandatory = $true)][string]$LocalAppData
    )
    if (-not (Test-GoalRouterLifecyclePathText $Path) -or $Path.StartsWith('\\') -or $Path -cnotmatch '\A[A-Za-z]:[\\/]') { throw 'refusing unsafe removal path' }
    $trimmed = $Path.TrimEnd('\', '/')
    if ($trimmed -cmatch '\A[A-Za-z]:\z') { throw 'refusing broad removal at a drive root' }
    foreach ($root in @($UserRoot, $AppData, $LocalAppData)) {
        $protected = $root.TrimEnd('\', '/')
        if (Test-GoalRouterWindowsPathContainsOrEqual -Parent $trimmed -Child $protected) { throw 'refusing broad removal at or above a user root' }
    }
    return $true
}

function Test-GoalRouterWindowsPathOverlap {
    param([string]$First, [string]$Second)
    $left = $First.TrimEnd('\', '/')
    $right = $Second.TrimEnd('\', '/')
    return (Test-GoalRouterWindowsPathContainsOrEqual -Parent $left -Child $right) -or (Test-GoalRouterWindowsPathContainsOrEqual -Parent $right -Child $left)
}

function Assert-GoalRouterUninstallManifestLayout {
    param([Parameter(Mandatory = $true)]$Manifest)
    Assert-GoalRouterExactProperties -Value $Manifest -Names @('manifest_version', 'protocol_version', 'version', 'launcher_version', 'image_reference', 'image_digest', 'image_platform', 'source_revision', 'owned', 'wsl_distribution', 'path_ownership', 'release_base') -Label 'trusted install control'
    Assert-GoalRouterExactProperties -Value $Manifest.owned -Names @('launcher', 'cmd', 'installer', 'uninstaller', 'install_root', 'bin_dir', 'config_file', 'config_dir', 'state_dir', 'codex_home') -Label 'trusted owned layout'
    Assert-GoalRouterExactProperties -Value $Manifest.path_ownership -Names @('installer_added', 'update_enabled', 'owned_value', 'before_state', 'before_value_kind', 'after_value_kind', 'after_sha256') -Label 'trusted PATH ownership'
    $owned = $Manifest.owned
    $expectedBin = Join-GoalRouterWindowsPath ([string]$owned.install_root) 'bin'
    foreach ($pair in @(
        @([string]$owned.bin_dir, $expectedBin),
        @([string]$owned.launcher, (Join-GoalRouterWindowsPath $expectedBin 'goalrouter.ps1')),
        @([string]$owned.cmd, (Join-GoalRouterWindowsPath $expectedBin 'goalrouter.cmd')),
        @([string]$owned.installer, (Join-GoalRouterWindowsPath $expectedBin 'install.ps1')),
        @([string]$owned.uninstaller, (Join-GoalRouterWindowsPath $expectedBin 'uninstall.ps1'))
    )) {
        if (-not (Test-GoalRouterWindowsPathEquivalent -First ([string]$pair[0]) -Second ([string]$pair[1]))) { throw 'trusted install control lifecycle file relationships are invalid' }
    }
    if (-not (Test-GoalRouterWindowsPathEquivalent -First ([string]$Manifest.path_ownership.owned_value) -Second ([string]$owned.bin_dir))) { throw 'trusted PATH ownership does not match the owned bin directory' }
    if (-not (Test-GoalRouterWindowsPathContainsOrEqual -Parent ([string]$owned.config_dir) -Child ([string]$owned.config_file)) -or (Test-GoalRouterWindowsPathEquivalent -First ([string]$owned.config_dir) -Second ([string]$owned.config_file))) { throw 'trusted config file relationship is invalid' }
}

function Assert-GoalRouterUninstallFileTarget {
    param([Parameter(Mandatory = $true)]$Info, [Parameter(Mandatory = $true)][string]$ExpectedPath, [bool]$AllowMissing)
    if (-not (Test-GoalRouterWindowsPathEquivalent -First ([string]$Info.Path) -Second $ExpectedPath) -or [string]$Info.ProviderName -cne 'FileSystem' -or -not (Test-GoalRouterWindowsPathEquivalent -First ([string]$Info.ProviderPath) -Second $ExpectedPath)) { throw "owned file target provider identity is unsafe: $ExpectedPath" }
    if (-not [bool]$Info.Exists) {
        if ($AllowMissing) { return }
        throw "owned file target is missing: $ExpectedPath"
    }
    if (-not [bool]$Info.IsLeaf -or [bool]$Info.IsContainer -or [bool]$Info.IsReparsePoint) { throw "owned file target is not a non-reparse regular file: $ExpectedPath" }
    if ($Info.PSObject.Properties.Name -contains 'OwnerMatchesCurrentUser' -and -not [bool]$Info.OwnerMatchesCurrentUser) { throw "owned file target is not owned by the current user: $ExpectedPath" }
    if ($Info.PSObject.Properties.Name -contains 'AclIsSafe' -and -not [bool]$Info.AclIsSafe) { throw "owned file target ACL is unsafe: $ExpectedPath" }
}

function New-GoalRouterUninstallRecoveryRecord {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('preserve', 'purge')][string]$Mode,
        [Parameter(Mandatory = $true)][ValidateSet('start', 'files', 'path', 'trees', 'final', 'cleanup')][string]$Phase,
        [Parameter(Mandatory = $true)]$Manifest
    )
    $manifestJson = ConvertTo-GoalRouterCanonicalJson $Manifest
    return [ordered]@{ recovery_version = 1; mode = $Mode; phase = $Phase; manifest_sha256 = Get-GoalRouterStringSha256 $manifestJson; manifest = $Manifest }
}

function New-GoalRouterUninstallPlan {
    param(
        [Parameter(Mandatory = $true)]$Manifest,
        [Parameter(Mandatory = $true)][bool]$Purge,
        [Parameter(Mandatory = $true)]$PathInfos,
        [Parameter(Mandatory = $true)]$CurrentUserPath,
        [AllowNull()][string]$RecoveryMode
    )
    $mode = if ($Purge) { 'purge' } else { 'preserve' }
    if (-not [string]::IsNullOrEmpty($RecoveryMode) -and $RecoveryMode -cne $mode) { throw 'uninstall recovery mode cannot change' }
    $owned = $Manifest.owned
    if (-not (Test-GoalRouterWindowsPathEquivalent -First ([string]$PathInfos.Config.Path) -Second ([string]$owned.config_dir)) -or -not (Test-GoalRouterWindowsPathEquivalent -First ([string]$PathInfos.State.Path) -Second ([string]$owned.state_dir))) { throw 'purge target is not the exact trusted recorded path' }
    foreach ($target in @(
        @{ Item = $PathInfos.Config; Allowed = @($script:GoalRouterDirectorySentinel, (Split-Path -Leaf ([string]$owned.config_file))) },
        @{ Item = $PathInfos.State; Allowed = @($script:GoalRouterDirectorySentinel, 'install.json', 'install.sha256', 'runs', 'reports') }
    )) {
        $item = $target.Item
        if ([string]$item.ProviderName -cne 'FileSystem' -or -not (Test-GoalRouterWindowsPathEquivalent -First ([string]$item.ProviderPath) -Second ([string]$item.Path))) { throw 'uninstall target provider path is unsafe' }
        if ([bool]$item.IsReparsePoint) { throw 'uninstall target is a reparse point' }
        if ($item.PSObject.Properties.Name -contains 'ContainsReparsePoint' -and [bool]$item.ContainsReparsePoint) { throw 'uninstall target contains a recursive reparse point' }
        if ($item.PSObject.Properties.Name -contains 'OwnerMatchesCurrentUser' -and -not [bool]$item.OwnerMatchesCurrentUser) { throw 'uninstall target is not owned by the current user' }
        if ($item.PSObject.Properties.Name -contains 'AclIsSafe' -and -not [bool]$item.AclIsSafe) { throw 'uninstall target ACL is unsafe' }
        if (-not [bool]$item.Exists -and -not [string]::IsNullOrEmpty($RecoveryMode)) { continue }
        if (-not [bool]$item.Exists -or -not [bool]$item.IsContainer) { throw 'uninstall target is missing or not a directory' }
        if ([string]::IsNullOrEmpty($RecoveryMode) -and [string]$item.Sentinel -cne $script:GoalRouterDirectorySentinelValue) { throw 'uninstall target ownership sentinel is invalid' }
        if ($Purge) { foreach ($entry in @($item.Entries)) { if ([string]$entry -cnotin @($target.Allowed)) { throw "purge target contains foreign content: $entry" } } }
    }
    if ($Purge) {
        if (Test-GoalRouterWindowsPathOverlap -First ([string]$owned.config_dir) -Second ([string]$owned.state_dir)) { throw 'purge targets overlap' }
    }
    $pathOwnership = [pscustomobject]@{
        InstallerAdded = [bool]$Manifest.path_ownership.installer_added
        OwnedValue = [string]$Manifest.path_ownership.owned_value
        BeforeState = [string]$Manifest.path_ownership.before_state
        BeforeValueKind = $Manifest.path_ownership.before_value_kind
        AfterValueKind = $Manifest.path_ownership.after_value_kind
        AfterSha256 = $Manifest.path_ownership.after_sha256
    }
    $pathResult = Remove-GoalRouterUserPathEntry -Snapshot $CurrentUserPath -Ownership $pathOwnership
    $removeTrees = if ($Purge) {
        @(
            if ([bool]$PathInfos.Config.Exists) { [string]$owned.config_dir }
            if ([bool]$PathInfos.State.Exists) { [string]$owned.state_dir }
        )
    } else { @() }
    $recovery = New-GoalRouterUninstallRecoveryRecord -Mode $mode -Phase 'start' -Manifest $Manifest
    return [pscustomobject]@{
        Mode = $mode
        RecoveryPath = Join-GoalRouterWindowsPath ([string]$owned.install_root) $script:GoalRouterRecoveryName
        Manifest = $Manifest
        EarlyFiles = @([string]$owned.launcher, [string]$owned.cmd, (Join-GoalRouterWindowsPath ([string]$owned.state_dir) 'install.json'), (Join-GoalRouterWindowsPath ([string]$owned.state_dir) 'install.sha256'))
        RemoveTrees = $removeTrees
        FinalFiles = @((Join-GoalRouterWindowsPath ([string]$owned.install_root) 'install.json'), (Join-GoalRouterWindowsPath ([string]$owned.install_root) 'install.sha256')) + $(if ($Purge) { @((Join-GoalRouterWindowsPath ([string]$owned.install_root) $script:GoalRouterDirectorySentinel)) } else { @() })
        InstallerPath = [string]$owned.installer
        UninstallerPath = [string]$owned.uninstaller
        PathResult = $pathResult
    }
}

function Invoke-GoalRouterUninstallCommit {
    param([Parameter(Mandatory = $true)]$Plan, [Parameter(Mandatory = $true)]$Ports)
    $replacePort = $Ports.Replace
    $removeFilePort = $Ports.RemoveFile
    $removeTreePort = $Ports.RemoveTree
    $setPathPort = $Ports.SetUserPath
    $writeJournal = {
        param([string]$Phase)
        $record = New-GoalRouterUninstallRecoveryRecord -Mode ([string]$Plan.Mode) -Phase $Phase -Manifest $Plan.Manifest
        $json = ConvertTo-GoalRouterCanonicalJson $record
        & $replacePort -Path ([string]$Plan.RecoveryPath) -Content $json
    }
    & $writeJournal -Phase 'start'
    foreach ($path in $Plan.EarlyFiles) { & $removeFilePort -Path ([string]$path) }
    & $writeJournal -Phase 'files'
    if ([bool]$Plan.PathResult.Changed) { & $setPathPort -Snapshot $Plan.PathResult.Snapshot }
    & $writeJournal -Phase 'path'
    foreach ($path in $Plan.RemoveTrees) { & $removeTreePort -Path ([string]$path) }
    & $writeJournal -Phase 'trees'
    & $writeJournal -Phase 'final'
    & $writeJournal -Phase 'cleanup'
    foreach ($path in $Plan.FinalFiles) { & $removeFilePort -Path ([string]$path) }
    & $removeFilePort -Path ([string]$Plan.RecoveryPath)
    & $removeFilePort -Path ([string]$Plan.InstallerPath)
    & $removeFilePort -Path ([string]$Plan.UninstallerPath)
}

function Invoke-GoalRouterWindowsUninstall {
    param(
        [string]$SelectedInstallRoot,
        [bool]$SelectedPurge,
        [bool]$Confirmed,
        [Parameter(Mandatory = $true)]$Ports,
        [Parameter(Mandatory = $true)][string]$PhysicalUninstallerPath
    )
    $getHostPort = $Ports.GetHost
    $snapshotPort = $Ports.Snapshot
    $getPathPort = $Ports.GetUserPath
    $getPathInfoPort = $Ports.GetPathInfo
    $resolvePathPort = $Ports.ResolvePath
    $hostInfo = & $getHostPort
    foreach ($hostRoot in @(
        @{ Path = [string]$hostInfo.UserProfile; Label = 'user profile root' },
        @{ Path = [string]$hostInfo.AppData; Label = 'roaming AppData root' },
        @{ Path = [string]$hostInfo.LocalAppData; Label = 'local AppData root' }
    )) {
        $hostRootInfo = & $resolvePathPort -Path ([string]$hostRoot.Path) -Kind 'Directory' -AllowMissing $false
        Assert-GoalRouterHostRoot -Info $hostRootInfo -Label ([string]$hostRoot.Label)
    }
    $installRootValue = if ([string]::IsNullOrEmpty($SelectedInstallRoot)) { Join-GoalRouterWindowsPath $hostInfo.LocalAppData 'GoalRouter' } else { $SelectedInstallRoot }
    [void](Assert-SafeGoalRouterRemoval -Path $installRootValue -UserRoot $hostInfo.UserProfile -AppData $hostInfo.AppData -LocalAppData $hostInfo.LocalAppData)
    $protectedRoots = @([string]$hostInfo.UserProfile, [string]$hostInfo.AppData, [string]$hostInfo.LocalAppData)
    $installRootTrustInfo = & $resolvePathPort -Path $installRootValue -Kind 'Directory' -AllowMissing $false
    Assert-GoalRouterLifecyclePathInfo -Info $installRootTrustInfo -Label 'install root deletion target' -AllowMissing $false -ProtectedRoots $protectedRoots -RequiredKind 'Directory'
    $installRootInfo = & $getPathInfoPort -Path $installRootValue
    if (-not $installRootInfo.Exists -or -not [bool]$installRootInfo.IsContainer -or [string]$installRootInfo.ProviderName -cne 'FileSystem' -or -not (Test-GoalRouterWindowsPathEquivalent -First ([string]$installRootInfo.ProviderPath) -Second $installRootValue) -or ([bool]$installRootInfo.IsReparsePoint) -or ($installRootInfo.PSObject.Properties.Name -contains 'ContainsReparsePoint' -and [bool]$installRootInfo.ContainsReparsePoint) -or ($installRootInfo.PSObject.Properties.Name -contains 'OwnerMatchesCurrentUser' -and -not [bool]$installRootInfo.OwnerMatchesCurrentUser) -or ($installRootInfo.PSObject.Properties.Name -contains 'AclIsSafe' -and -not [bool]$installRootInfo.AclIsSafe)) { throw 'trusted install root provider, ownership, ACL, or recursive reparse state is unsafe' }
    $manifestPath = Join-GoalRouterWindowsPath $installRootValue 'install.json'
    $recoveryPath = Join-GoalRouterWindowsPath $installRootValue $script:GoalRouterRecoveryName
    $recoverySnapshot = & $snapshotPort -Path $recoveryPath
    $recoveryMode = $null
    $recoveryPhase = $null
    $trustedJson = $null
    if ($recoverySnapshot.Present) {
        try { $recovery = [string]$recoverySnapshot.Content | ConvertFrom-Json -ErrorAction Stop }
        catch { throw 'trusted uninstall recovery marker is corrupt' }
        Assert-GoalRouterExactProperties -Value $recovery -Names @('recovery_version', 'mode', 'phase', 'manifest_sha256', 'manifest') -Label 'uninstall recovery'
        if ([int]$recovery.recovery_version -ne 1 -or [string]$recovery.mode -cnotin @('preserve', 'purge') -or [string]$recovery.phase -cnotin @('start', 'files', 'path', 'trees', 'final', 'cleanup')) { throw 'trusted uninstall recovery marker is invalid' }
        $manifest = $recovery.manifest
        $recoveryMode = [string]$recovery.mode
        $recoveryPhase = [string]$recovery.phase
        $trustedJson = ConvertTo-GoalRouterCanonicalInstallManifestJson $manifest
        if ([string]$recovery.manifest_sha256 -cnotmatch '\A[0-9a-f]{64}\z' -or (Get-GoalRouterStringSha256 $trustedJson) -cne [string]$recovery.manifest_sha256) { throw 'trusted uninstall recovery manifest checksum is invalid' }
        $manifestSnapshot = [pscustomobject]@{ Present = $true; Content = $trustedJson }
        $checksumSnapshot = [pscustomobject]@{ Present = $true; Content = (Get-GoalRouterStringSha256 $trustedJson) + "`n" }
    } else {
        $manifestSnapshot = & $snapshotPort -Path $manifestPath
        if (-not $manifestSnapshot.Present) {
            if ((& $snapshotPort -Path (Join-GoalRouterWindowsPath $installRootValue 'install.sha256')).Present) { throw 'trusted install control is incomplete before bounded self cleanup' }
            $expectedPhysicalUninstaller = Join-GoalRouterWindowsPath (Join-GoalRouterWindowsPath $installRootValue 'bin') 'uninstall.ps1'
            if (-not (Test-GoalRouterWindowsPathEquivalent -First $PhysicalUninstallerPath -Second $expectedPhysicalUninstaller)) { throw 'trusted install control is missing' }
            foreach ($residual in @('goalrouter.ps1', 'goalrouter.cmd')) {
                if ((& $snapshotPort -Path (Join-GoalRouterWindowsPath (Join-GoalRouterWindowsPath $installRootValue 'bin') $residual)).Present) { throw 'trusted install control is missing before bounded self cleanup' }
            }
            & $Ports.RemoveFile -Path (Join-GoalRouterWindowsPath (Join-GoalRouterWindowsPath $installRootValue 'bin') 'install.ps1')
            & $Ports.RemoveFile -Path $PhysicalUninstallerPath
            [Console]::Out.WriteLine('GoalRouter final uninstaller cleanup completed')
            return
        }
        try { $manifest = [string]$manifestSnapshot.Content | ConvertFrom-Json -ErrorAction Stop }
        catch { throw 'trusted install control is corrupt' }
        $trustedJson = [string]$manifestSnapshot.Content
        $checksumSnapshot = & $snapshotPort -Path (Join-GoalRouterWindowsPath $installRootValue 'install.sha256')
        if (-not $checksumSnapshot.Present -or ([string]$checksumSnapshot.Content).Trim() -cnotmatch '\A[0-9a-f]{64}\z' -or (Get-GoalRouterStringSha256 $trustedJson) -cne ([string]$checksumSnapshot.Content).Trim()) { throw 'trusted install control checksum is invalid' }
    }
    $rootSentinelPresent = @($installRootInfo.Entries) -ccontains $script:GoalRouterDirectorySentinel
    if ($rootSentinelPresent -and [string]$installRootInfo.Sentinel -cne $script:GoalRouterDirectorySentinelValue) { throw 'trusted install root ownership sentinel is invalid' }
    $rootSentinelMayBeRemoved = $recoveryMode -ceq 'purge' -and $recoveryPhase -ceq 'cleanup'
    if (-not $rootSentinelPresent -and -not $rootSentinelMayBeRemoved) { throw 'trusted install root ownership sentinel is missing' }
    if ([int]$manifest.manifest_version -ne 1 -or [int]$manifest.protocol_version -ne 1) { throw 'trusted install control version is invalid' }
    Assert-GoalRouterUninstallManifestLayout -Manifest $manifest
    $semanticLayout = [pscustomobject]@{ InstallRoot = [string]$manifest.owned.install_root; BinDir = [string]$manifest.owned.bin_dir; ConfigFile = [string]$manifest.owned.config_file; ConfigDir = [string]$manifest.owned.config_dir; StateDir = [string]$manifest.owned.state_dir; CodexHome = [string]$manifest.owned.codex_home; LauncherPath = [string]$manifest.owned.launcher; CmdPath = [string]$manifest.owned.cmd; InstallerPath = [string]$manifest.owned.installer; UninstallerPath = [string]$manifest.owned.uninstaller }
    Assert-GoalRouterExistingInstallManifest -Manifest $manifest -Json $trustedJson -Layout $semanticLayout
    if (-not (Test-GoalRouterWindowsPathEquivalent -First ([string]$manifest.owned.install_root) -Second $installRootValue)) { throw 'trusted install root does not match the requested root' }
    if (-not (Test-GoalRouterWindowsPathEquivalent -First ([string]$manifest.owned.uninstaller) -Second $PhysicalUninstallerPath)) { throw 'physical uninstaller path does not match trusted install control' }
    if ($SelectedPurge) {
        foreach ($purgePath in @([string]$manifest.owned.config_dir, [string]$manifest.owned.state_dir)) {
            [void](Assert-SafeGoalRouterRemoval -Path $purgePath -UserRoot $hostInfo.UserProfile -AppData $hostInfo.AppData -LocalAppData $hostInfo.LocalAppData)
        }
    }
    if (-not $recoverySnapshot.Present) {
        if (-not $Confirmed) { throw 'uninstall requires -Yes' }
        $stateParityPath = Join-GoalRouterWindowsPath ([string]$manifest.owned.state_dir) 'install.json'
        $stateSnapshot = & $snapshotPort -Path $stateParityPath
        Assert-GoalRouterTrustedStateParity -TrustedJson $trustedJson -StateJson $(if ($stateSnapshot.Present) { [string]$stateSnapshot.Content } else { $null })
        $stateChecksumSnapshot = & $snapshotPort -Path (Join-GoalRouterWindowsPath ([string]$manifest.owned.state_dir) 'install.sha256')
        if (-not $stateChecksumSnapshot.Present -or [string]$stateChecksumSnapshot.Content -cne [string]$checksumSnapshot.Content) { throw 'runtime state control checksum parity is invalid' }
    }
    $pathInfos = @{
        Config = & $getPathInfoPort -Path ([string]$manifest.owned.config_dir)
        State = & $getPathInfoPort -Path ([string]$manifest.owned.state_dir)
    }
    $currentUserPath = & $getPathPort
    $plan = New-GoalRouterUninstallPlan -Manifest $manifest -Purge $SelectedPurge -PathInfos $pathInfos -CurrentUserPath $currentUserPath -RecoveryMode $recoveryMode
    foreach ($ownedFile in @($plan.EarlyFiles) + @($plan.FinalFiles) + @($plan.RecoveryPath, $plan.InstallerPath, $plan.UninstallerPath)) {
        $ownedFileInfo = & $getPathInfoPort -Path ([string]$ownedFile)
        $allowMissingOwnedFile = if (Test-GoalRouterWindowsPathEquivalent -First ([string]$ownedFile) -Second ([string]$plan.RecoveryPath)) { -not $recoverySnapshot.Present } else { $recoverySnapshot.Present }
        Assert-GoalRouterUninstallFileTarget -Info $ownedFileInfo -ExpectedPath ([string]$ownedFile) -AllowMissing $allowMissingOwnedFile
    }
    foreach ($ownedFile in @($plan.EarlyFiles) + @($plan.FinalFiles) + @($plan.RecoveryPath, $plan.InstallerPath, $plan.UninstallerPath)) {
        $allowMissingOwnedFile = if (Test-GoalRouterWindowsPathEquivalent -First ([string]$ownedFile) -Second ([string]$plan.RecoveryPath)) { -not $recoverySnapshot.Present } else { $recoverySnapshot.Present }
        $trustedFileInfo = & $resolvePathPort -Path ([string]$ownedFile) -Kind 'File' -AllowMissing $allowMissingOwnedFile
        if ($trustedFileInfo.PSObject.Properties.Name -notcontains 'AncestorChainIsSafe' -or -not [bool]$trustedFileInfo.AncestorChainIsSafe) { throw 'uninstall file deletion target ancestor chain is unsafe' }
        Assert-GoalRouterLifecyclePathInfo -Info $trustedFileInfo -Label 'uninstall file deletion target' -AllowMissing $allowMissingOwnedFile -ProtectedRoots $protectedRoots -RequiredKind 'File'
    }
    foreach ($ownedTree in @($plan.RemoveTrees)) {
        $trustedTreeInfo = & $resolvePathPort -Path ([string]$ownedTree) -Kind 'Directory' -AllowMissing $false
        if ($trustedTreeInfo.PSObject.Properties.Name -notcontains 'AncestorChainIsSafe' -or -not [bool]$trustedTreeInfo.AncestorChainIsSafe) { throw 'uninstall tree deletion target ancestor chain is unsafe' }
        Assert-GoalRouterLifecyclePathInfo -Info $trustedTreeInfo -Label 'uninstall tree deletion target' -AllowMissing $false -ProtectedRoots $protectedRoots -RequiredKind 'Directory'
    }
    Invoke-GoalRouterUninstallCommit -Plan $plan -Ports $Ports
    [Console]::Out.WriteLine("GoalRouter uninstalled in $($plan.Mode) mode")
}

if ($goalRouterUninstallerIsDotSourced) { return }

try {
    Invoke-GoalRouterWindowsUninstall -SelectedInstallRoot $selectedInstallRootArgument -SelectedPurge ([bool]$Purge) -Confirmed $selectedConfirmedArgument -Ports (New-GoalRouterProductionLifecyclePorts) -PhysicalUninstallerPath $PSCommandPath
} catch {
    [Console]::Error.WriteLine("goalrouter uninstaller: $($_.Exception.Message)")
    exit 1
}
