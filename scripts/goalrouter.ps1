# SPDX-License-Identifier: MIT
# File: scripts/goalrouter.ps1
# Purpose: Windows launcher for the GoalRouter runtime container
#requires -Version 5.1

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ArgumentList
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$WarningPreference = 'Stop'

$script:GoalRouterPhysicalPathSecurityVerifier = { param([string]$Path); Get-GoalRouterTrustedWindowsPathSecurity -Path $Path }
$script:GoalRouterPhysicalAncestorSecurityVerifier = { param([string]$Path); Get-GoalRouterTrustedWindowsPathSecurity -Path $Path -AllowTrustedOwner $true }

$script:GoalRouterNativeInvoker = {
    param([string]$FilePath, [string[]]$Arguments, [bool]$CaptureOutput)
    $nativeOutput = @(& $FilePath @Arguments 2>&1)
    $nativeExitCode = $LASTEXITCODE
    $nativeErrors = @($nativeOutput | Where-Object { $_ -is [Management.Automation.ErrorRecord] })
    if ($nativeExitCode -eq 0 -and $nativeErrors.Count -gt 0) { throw 'native command emitted stderr despite a successful exit code' }
    if (-not $CaptureOutput) { foreach ($line in $nativeOutput) { [Console]::Out.WriteLine($line) } }
    return [pscustomobject]@{ ExitCode = $nativeExitCode; Output = if ($CaptureOutput) { $nativeOutput } else { $null } }
}

$script:GoalRouterPathResolver = {
    param([string]$Path)
    $resolvedItems = @(Resolve-Path -LiteralPath $Path -ErrorAction Stop)
    $results = @()
    foreach ($resolved in $resolvedItems) {
        $providerPath = $resolved.ProviderPath
        $results += [pscustomobject]@{
            ProviderName = $resolved.Provider.Name
            ProviderPath = $providerPath
            IsContainer = Test-Path -LiteralPath $providerPath -PathType Container
            IsLeaf = Test-Path -LiteralPath $providerPath -PathType Leaf
        }
    }
    return $results
}

function Initialize-GoalRouterNativeEnvironment {
    if ($null -ne ('GoalRouter.NativeEnvironment' -as [type])) { return }
    $typeDefinition = @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
namespace GoalRouter {
    public sealed class ProcessEnvironmentValue {
        public bool Present { get; private set; }
        public string Value { get; private set; }
        public ProcessEnvironmentValue(bool present, string value) {
            Present = present;
            Value = value;
        }
    }
    public static class NativeEnvironment {
        private const int ERROR_ENVVAR_NOT_FOUND = 203;
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, ExactSpelling = true)]
        private static extern uint GetEnvironmentVariableW(string name, StringBuilder value, uint size);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, ExactSpelling = true)]
        private static extern bool SetEnvironmentVariableW(string name, string value);
        [DllImport("kernel32.dll", ExactSpelling = true)]
        private static extern void SetLastError(uint errorCode);

        public static ProcessEnvironmentValue ClassifyZeroResult(int error) {
            if (error == 0) {
                return new ProcessEnvironmentValue(true, String.Empty);
            }
            if (error == ERROR_ENVVAR_NOT_FOUND) {
                return new ProcessEnvironmentValue(false, null);
            }
            throw new Win32Exception(error);
        }

        public static ProcessEnvironmentValue Get(string name) {
            SetLastError(0);
            uint required = GetEnvironmentVariableW(name, null, 0);
            if (required == 0) {
                return ClassifyZeroResult(Marshal.GetLastWin32Error());
            }
            StringBuilder buffer = new StringBuilder((int)required);
            SetLastError(0);
            uint written = GetEnvironmentVariableW(name, buffer, (uint)buffer.Capacity);
            if (written == 0) {
                return ClassifyZeroResult(Marshal.GetLastWin32Error());
            }
            if (written >= buffer.Capacity) {
                throw new Win32Exception("Environment variable changed while it was being read.");
            }
            return new ProcessEnvironmentValue(true, buffer.ToString());
        }

        public static void Set(string name, string value) {
            if (!SetEnvironmentVariableW(name, value)) {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }
        }
    }
}
'@
    Add-Type -TypeDefinition $typeDefinition -ErrorAction Stop
}

$script:GoalRouterEnvironmentAccessor = {
    param([string]$Operation, [string]$Name, [AllowNull()][string]$Value)
    $goalRouterRunsOnWindows = [Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT
    if ($Operation -ceq 'Get') {
        if ($goalRouterRunsOnWindows) {
            Initialize-GoalRouterNativeEnvironment
            return [GoalRouter.NativeEnvironment]::Get($Name)
        }
        $variables = [Environment]::GetEnvironmentVariables()
        if ($variables.Contains($Name)) {
            return [pscustomobject]@{ Present = $true; Value = [string]$variables[$Name] }
        }
        return [pscustomobject]@{ Present = $false; Value = $null }
    }
    if ($goalRouterRunsOnWindows) {
        Initialize-GoalRouterNativeEnvironment
        $nativeValue = if ($Operation -ceq 'Remove') { $null } else { $Value }
        [GoalRouter.NativeEnvironment]::Set($Name, $nativeValue)
        return
    }
    if ($Operation -ceq 'Set') { [Environment]::SetEnvironmentVariable($Name, $Value); return }
    if ($Operation -ceq 'Remove') { [Environment]::SetEnvironmentVariable($Name, $null); return }
    throw "unknown environment operation: $Operation"
}

function Invoke-GoalRouterNative {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [bool]$CaptureOutput = $false
    )
    return & $script:GoalRouterNativeInvoker -FilePath $FilePath -Arguments $Arguments -CaptureOutput $CaptureOutput
}

function Get-GoalRouterProcessEnvironmentValue {
    param([Parameter(Mandatory = $true)][string]$Name)
    return & $script:GoalRouterEnvironmentAccessor -Operation 'Get' -Name $Name -Value $null
}

function Set-GoalRouterProcessEnvironmentValue {
    param([Parameter(Mandatory = $true)][string]$Name, [AllowEmptyString()][string]$Value)
    & $script:GoalRouterEnvironmentAccessor -Operation 'Set' -Name $Name -Value $Value
}

function Remove-GoalRouterProcessEnvironmentValue {
    param([Parameter(Mandatory = $true)][string]$Name)
    & $script:GoalRouterEnvironmentAccessor -Operation 'Remove' -Name $Name -Value $null
}

function Get-GoalRouterHelp {
    return @'
Usage: goalrouter [launcher-options] <command> [command-options]

Launcher options:
  --project <host-path>
  --access readonly|write|docker
  --config <host-path>
  --state-dir <host-path>
  --codex-home <host-path>
  --image <tag-or-digest>
  --auth-mode existing-session|api-key
  --json

Maintenance commands:
  doctor
  update
  version
  uninstall
'@
}

function ConvertFrom-GoalRouterArguments {
    param([string[]]$Arguments)

    $result = [ordered]@{
        Project = $null
        Access = 'readonly'
        Config = $null
        StateDir = $null
        CodexHome = $null
        Image = $null
        ImageIsExplicit = $false
        AuthMode = 'existing-session'
        Json = $false
        Help = $false
        Forwarded = @()
    }
    $index = 0
    while ($index -lt $Arguments.Count) {
        $argument = $Arguments[$index]
        if ($argument -ceq '--json') {
            $result.Json = $true
            $index++
            continue
        }
        if ($argument -ceq '--help') {
            $result.Help = $true
            return [pscustomobject]$result
        }
        $valueOptions = @('--project', '--access', '--config', '--state-dir', '--codex-home', '--image', '--auth-mode')
        if ($argument -cin $valueOptions) {
            if ($index + 1 -ge $Arguments.Count) { throw "$argument requires a value" }
            $value = $Arguments[$index + 1]
            switch -CaseSensitive ($argument) {
                '--project' { $result.Project = $value }
                '--access' { $result.Access = $value }
                '--config' { $result.Config = $value }
                '--state-dir' { $result.StateDir = $value }
                '--codex-home' { $result.CodexHome = $value }
                '--image' { $result.Image = $value; $result.ImageIsExplicit = $true }
                '--auth-mode' { $result.AuthMode = $value }
            }
            $index += 2
            continue
        }
        if ($argument.StartsWith('--')) { throw "unknown launcher option: $argument" }
        $result.Forwarded = @($Arguments[$index..($Arguments.Count - 1)])
        break
    }
    if ($result.Access -cnotin @('readonly', 'write', 'docker')) { throw "invalid --access: $($result.Access)" }
    if ($result.AuthMode -cnotin @('existing-session', 'api-key')) { throw "invalid --auth-mode: $($result.AuthMode)" }
    return [pscustomobject]$result
}

function Test-GoalRouterSingleLine {
    param([AllowEmptyString()][string]$Value)
    return $Value -cmatch '\A[\x20-\x7e]+\z'
}

function Test-GoalRouterWslDistribution {
    param([AllowEmptyString()][string]$Value)
    return (Test-GoalRouterSingleLine $Value) -and -not $Value.StartsWith('-')
}

function Assert-GoalRouterTrustedReleaseUri {
    param([Parameter(Mandatory = $true)][string]$Uri)
    if (-not (Test-GoalRouterSingleLine $Uri) -or $Uri.Contains('\')) { throw 'trusted release URI is invalid' }
    $parsed = $null
    if (-not [Uri]::TryCreate($Uri, [UriKind]::Absolute, [ref]$parsed) -or [string]::IsNullOrEmpty($parsed.Host) -or [string]::IsNullOrEmpty($parsed.Authority)) { throw 'trusted release URI is invalid' }
    if (-not [string]::IsNullOrEmpty($parsed.UserInfo) -or -not [string]::IsNullOrEmpty($parsed.Query) -or -not [string]::IsNullOrEmpty($parsed.Fragment)) { throw 'trusted release URI is invalid' }
    if ($parsed.Scheme -ceq 'https') { return $Uri }
    if ($parsed.Scheme -ceq 'http' -and $parsed.Host -cin @('127.0.0.1', 'localhost', '::1', '[::1]')) { return $Uri }
    throw 'trusted release URI is invalid'
}

function Test-GoalRouterPathText {
    param([AllowEmptyString()][string]$Value)
    return -not [string]::IsNullOrEmpty($Value) -and $Value -cnotmatch '[\x00-\x1f\x7f]'
}

function Test-GoalRouterDigest {
    param([string]$Digest)
    return (Test-GoalRouterSingleLine $Digest) -and ($Digest -cmatch '\Asha256:[0-9a-f]{64}\z')
}

function Test-GoalRouterRegistryDomain {
    param([string]$Domain)
    $domainComponent = '(?:[A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9-]*[A-Za-z0-9])'
    $domainPattern = "\A$domainComponent(?:[.]$domainComponent)*(?::[0-9]+)?\z"
    $ipv6Pattern = '\A\[[A-Fa-f0-9:]+\](?::[0-9]+)?\z'
    return ($Domain -cmatch $domainPattern) -or ($Domain -cmatch $ipv6Pattern)
}

function Test-GoalRouterNamedImage {
    param([string]$Image)
    if (-not (Test-GoalRouterSingleLine $Image)) { return $false }
    if ($Image.StartsWith('-') -or $Image.Contains('@')) { return $false }

    $name = $Image
    $lastSlash = $name.LastIndexOf('/')
    $lastComponent = $name.Substring($lastSlash + 1)
    if ($lastComponent.Contains(':')) {
        $tagSeparator = $name.LastIndexOf(':')
        $tag = $name.Substring($tagSeparator + 1)
        if ($tag -cnotmatch '\A[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\z') { return $false }
        $name = $name.Substring(0, $tagSeparator)
    }
    if ($name.Length -gt 255) { return $false }

    $firstSlash = $name.IndexOf('/')
    $remote = $name
    if ($firstSlash -ge 0) {
        $first = $name.Substring(0, $firstSlash)
        if ($first -ceq 'localhost' -or $first.Contains('.') -or $first.Contains(':') -or $first.StartsWith('[')) {
            if (-not (Test-GoalRouterRegistryDomain $first)) { return $false }
            $remote = $name.Substring($firstSlash + 1)
        }
    }
    $component = '[a-z0-9]+(?:(?:[.]|__|_|-+)[a-z0-9]+)*'
    return $remote -cmatch "\A$component(?:/$component)*\z"
}

function Test-GoalRouterImageOverride {
    param([AllowEmptyString()][string]$Image)
    if (-not (Test-GoalRouterSingleLine $Image)) { return $false }
    $at = $Image.IndexOf('@')
    if ($at -lt 0) { return Test-GoalRouterNamedImage $Image }
    if ($at -ne $Image.LastIndexOf('@')) { return $false }
    $name = $Image.Substring(0, $at)
    $digest = $Image.Substring($at + 1)
    return (Test-GoalRouterNamedImage $name) -and (Test-GoalRouterDigest $digest)
}

function Read-GoalRouterMetadata {
    param([string]$Path, [string]$Name)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "missing ${Name}: $Path" }
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    if ($bytes.Count -eq 0) { throw "invalid $Name metadata bytes" }
    foreach ($byte in $bytes) {
        if ($byte -lt 0x20 -or $byte -gt 0x7e) { throw "invalid $Name metadata bytes" }
    }
    return [System.Text.Encoding]::ASCII.GetString($bytes)
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

function Read-GoalRouterTrustedUtf8Text {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Label)
    return ConvertFrom-GoalRouterStrictUtf8Bytes -Bytes ([IO.File]::ReadAllBytes($Path)) -Label $Label
}

function Resolve-GoalRouterHostPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][ValidateSet('Directory', 'File')][string]$Kind,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$Distribution
    )
    if (-not (Test-GoalRouterPathText $Path)) { throw "$Label path contains a control character" }
    if ($Path.StartsWith('\\')) { throw "$Label path cannot be a UNC path: $Path" }
    if ($Path -match '\A[^\\/:]+::') { throw "$Label path must use the FileSystem provider: $Path" }
    if ($Path -cnotmatch '\A[A-Za-z]:[\\/]') { throw "$label path must be an absolute local drive path: $Path" }

    try { $resolvedItems = @(& $script:GoalRouterPathResolver -Path $Path) }
    catch { throw "$label $($Kind.ToLowerInvariant()) does not exist: $Path" }
    if ($resolvedItems.Count -ne 1) { throw "$label path must resolve to exactly one item: $Path" }
    $resolved = $resolvedItems[0]
    if ($resolved.ProviderName -cne 'FileSystem') { throw "$label path must use the FileSystem provider: $Path" }
    $providerPath = [string]$resolved.ProviderPath
    if ($providerPath.StartsWith('\\')) { throw "$label provider-native path cannot be a UNC path: $providerPath" }
    if ($providerPath -cnotmatch '\A[A-Za-z]:[\\/]') { throw "$label provider-native path must be an absolute local drive path: $providerPath" }
    if ($Kind -ceq 'Directory' -and -not $resolved.IsContainer) { throw "$label directory does not exist: $Path" }
    if ($Kind -ceq 'File' -and -not $resolved.IsLeaf) { throw "$label file does not exist: $Path" }

    $windowsPath = $providerPath.Replace('/', '\')
    $nativeArguments = @('-d', $Distribution, '--exec', 'wslpath', '-a', '-u', '--', $windowsPath)
    $nativeResult = Invoke-GoalRouterNative -FilePath 'wsl.exe' -Arguments $nativeArguments -CaptureOutput $true
    if ([int]$nativeResult.ExitCode -ne 0) { throw "wslpath failed for $label with exit code $($nativeResult.ExitCode)" }
    $output = @($nativeResult.Output)
    if ($output.Count -ne 1 -or -not (Test-GoalRouterPathText ([string]$output[0])) -or -not ([string]$output[0]).StartsWith('/')) { throw "wslpath returned an invalid $label path" }
    return [pscustomobject]@{ ProviderPath = $windowsPath; WslPath = [string]$output[0] }
}

function Resolve-GoalRouterPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][ValidateSet('Directory', 'File')][string]$Kind,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$Distribution
    )
    return (Resolve-GoalRouterHostPath -Path $Path -Kind $Kind -Label $Label -Distribution $Distribution).WslPath
}

function Get-GoalRouterInstalledDistribution {
    param([string]$LocalAppData)
    if ([string]::IsNullOrEmpty($LocalAppData)) { throw 'LOCALAPPDATA is required' }
    $metadataPath = Join-Path (Join-Path $LocalAppData 'GoalRouter') 'install.json'
    if (-not (Test-Path -LiteralPath $metadataPath -PathType Leaf)) { throw "missing install metadata: $metadataPath" }
    try { $metadata = Get-Content -LiteralPath $metadataPath -Raw | ConvertFrom-Json }
    catch { throw "invalid install metadata: $metadataPath" }
    if ($null -eq $metadata.wsl_distribution -or -not (Test-GoalRouterWslDistribution ([string]$metadata.wsl_distribution))) {
        throw 'install metadata has an invalid wsl_distribution'
    }
    return [string]$metadata.wsl_distribution
}

function Assert-GoalRouterTrustedStateParity {
    param([Parameter(Mandatory = $true)][string]$TrustedJson, [AllowNull()][string]$StateJson)
    if ([string]::IsNullOrEmpty($StateJson)) { throw 'runtime state parity manifest is missing' }
    if ($TrustedJson -cne $StateJson) { throw 'runtime state parity does not match trusted install control' }
}

function Test-GoalRouterWindowsPathEquivalent {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$First, [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Second)
    return $First.Replace('/', '\').TrimEnd('\') -ieq $Second.Replace('/', '\').TrimEnd('\')
}

function Get-GoalRouterTrustedWindowsPathSecurity {
    param([Parameter(Mandatory = $true)][string]$Path, [bool]$AllowTrustedOwner = $false)
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
    $ownerSid = if ([string]$acl.Owner -match '\AS-\d(?:-\d+)+\z') { [string]$acl.Owner } else { ([Security.Principal.NTAccount]$acl.Owner).Translate([Security.Principal.SecurityIdentifier]).Value }
    $allowedSids = @($identity.User.Value, 'S-1-5-18', 'S-1-5-32-544', 'S-1-3-0')
    $unsafe = @($acl.Access | Where-Object {
        $sid = $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        $_.AccessControlType -ceq [Security.AccessControl.AccessControlType]::Allow -and (Test-GoalRouterLauncherAclRightsUnsafe -Rights ([long]$_.FileSystemRights)) -and $sid -notin $allowedSids
    }).Count -gt 0
    $ownerIsAllowed = if ($AllowTrustedOwner) { $ownerSid -in $allowedSids } else { $ownerSid -ceq $identity.User.Value }
    if (-not $ownerIsAllowed -or $unsafe) { throw "trusted path ownership or ACL is unsafe: $Path" }
}

function Get-GoalRouterLauncherMutationRightsMask {
    return [long]([Security.AccessControl.FileSystemRights]::WriteData -bor [Security.AccessControl.FileSystemRights]::AppendData -bor [Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor [Security.AccessControl.FileSystemRights]::WriteAttributes -bor [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor [Security.AccessControl.FileSystemRights]::Delete -bor [Security.AccessControl.FileSystemRights]::ChangePermissions -bor [Security.AccessControl.FileSystemRights]::TakeOwnership)
}

function Test-GoalRouterLauncherAclRightsUnsafe {
    param([Parameter(Mandatory = $true)][long]$Rights)
    return ($Rights -band (Get-GoalRouterLauncherMutationRightsMask)) -ne 0
}

function ConvertTo-GoalRouterCanonicalInstalledManifestJson {
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
    return $canonical | ConvertTo-Json -Compress -Depth 10
}

function Assert-GoalRouterTrustedPhysicalAncestorChain {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Label)
    $cursor = $Path
    while (-not [string]::IsNullOrEmpty($cursor)) {
        if ($cursor.Replace('/', '\').TrimEnd('\') -cmatch '\A[A-Za-z]:\z') { break }
        if (([IO.File]::GetAttributes($cursor) -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "$Label ancestor chain contains a reparse point" }
        & $script:GoalRouterPhysicalAncestorSecurityVerifier -Path $cursor
        $next = Split-Path -Parent $cursor
        if ([string]::IsNullOrEmpty($next) -or $next -ceq $cursor) { break }
        $cursor = $next
    }
}

function Assert-GoalRouterTrustedPhysicalLeaf {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Label)
    $resolved = @(Resolve-Path -LiteralPath $Path -ErrorAction Stop)
    if ($resolved.Count -ne 1 -or [string]$resolved[0].Provider.Name -cne 'FileSystem' -or -not (Test-GoalRouterWindowsPathEquivalent -First ([string]$resolved[0].ProviderPath) -Second $Path) -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "$Label is not an exact FileSystem leaf" }
    if (([IO.File]::GetAttributes($Path) -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "$Label is a reparse point" }
    & $script:GoalRouterPhysicalPathSecurityVerifier -Path ([string]$resolved[0].ProviderPath)
    Assert-GoalRouterTrustedPhysicalAncestorChain -Path (Split-Path -Parent ([string]$resolved[0].ProviderPath)) -Label $Label
    return [string]$resolved[0].ProviderPath
}

function Assert-GoalRouterTrustedPhysicalDirectory {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Label)
    $resolved = @(Resolve-Path -LiteralPath $Path -ErrorAction Stop)
    if ($resolved.Count -ne 1 -or [string]$resolved[0].Provider.Name -cne 'FileSystem' -or -not (Test-GoalRouterWindowsPathEquivalent -First ([string]$resolved[0].ProviderPath) -Second $Path) -or -not (Test-Path -LiteralPath $Path -PathType Container)) { throw "$Label is not an exact FileSystem directory" }
    if (([IO.File]::GetAttributes($Path) -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "$Label is a reparse point" }
    if (@(Get-ChildItem -LiteralPath $Path -Force -Recurse -ErrorAction Stop | Where-Object { ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 }).Count -gt 0) { throw "$Label contains a recursive reparse point" }
    & $script:GoalRouterPhysicalPathSecurityVerifier -Path ([string]$resolved[0].ProviderPath)
    Assert-GoalRouterTrustedPhysicalAncestorChain -Path (Split-Path -Parent ([string]$resolved[0].ProviderPath)) -Label $Label
    return [string]$resolved[0].ProviderPath
}

function Test-GoalRouterCodexSessionFileEntry {
    param(
        [Parameter(Mandatory = $true)][string]$Directory,
        [Parameter(Mandatory = $true)][ValidateSet('auth.json', 'config.toml', 'models_cache.json')][string]$Name
    )
    $expectedPath = Join-Path $Directory $Name
    foreach ($entry in [IO.Directory]::EnumerateFileSystemEntries($Directory, $Name, [IO.SearchOption]::TopDirectoryOnly)) {
        if (Test-GoalRouterWindowsPathEquivalent -First ([string]$entry) -Second $expectedPath) { return $true }
    }
    return $false
}

function Assert-GoalRouterTrustedCodexSessionDirectory {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Label)
    $resolved = @(Resolve-Path -LiteralPath $Path -ErrorAction Stop)
    if ($resolved.Count -ne 1 -or [string]$resolved[0].Provider.Name -cne 'FileSystem' -or -not (Test-GoalRouterWindowsPathEquivalent -First ([string]$resolved[0].ProviderPath) -Second $Path) -or -not (Test-Path -LiteralPath $Path -PathType Container)) { throw "$Label is not an exact FileSystem directory" }
    if (([IO.File]::GetAttributes($Path) -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "$Label is a reparse point" }
    $physicalPath = [string]$resolved[0].ProviderPath
    & $script:GoalRouterPhysicalPathSecurityVerifier -Path $physicalPath
    Assert-GoalRouterTrustedPhysicalAncestorChain -Path (Split-Path -Parent $physicalPath) -Label $Label
    foreach ($name in @('auth.json', 'config.toml', 'models_cache.json')) {
        $sessionFile = Join-Path $physicalPath $name
        if (Test-GoalRouterCodexSessionFileEntry -Directory $physicalPath -Name $name) { [void](Assert-GoalRouterTrustedPhysicalLeaf -Path $sessionFile -Label "$Label $name") }
    }
    return $physicalPath
}

function Assert-GoalRouterInstalledManifestSchema {
    param([Parameter(Mandatory = $true)]$Manifest, [AllowNull()][string]$TrustedJson = $null)
    foreach ($schema in @(
        @{ Value = $Manifest; Names = @('image_digest', 'image_platform', 'image_reference', 'launcher_version', 'manifest_version', 'owned', 'path_ownership', 'protocol_version', 'release_base', 'source_revision', 'version', 'wsl_distribution'); Label = 'trusted install control' },
        @{ Value = $Manifest.owned; Names = @('launcher', 'cmd', 'installer', 'uninstaller', 'install_root', 'bin_dir', 'config_file', 'config_dir', 'state_dir', 'codex_home'); Label = 'trusted owned layout' },
        @{ Value = $Manifest.path_ownership; Names = @('installer_added', 'update_enabled', 'owned_value', 'before_state', 'before_value_kind', 'after_value_kind', 'after_sha256'); Label = 'trusted PATH ownership' }
    )) {
        $actual = @($schema.Value.PSObject.Properties.Name | Sort-Object)
        $expected = @($schema.Names | Sort-Object)
        if ($actual.Count -ne $expected.Count) { throw "$($schema.Label) schema is invalid" }
        for ($index = 0; $index -lt $expected.Count; $index++) { if ($actual[$index] -cne $expected[$index]) { throw "$($schema.Label) schema is invalid" } }
    }
    $owned = $Manifest.owned
    $expectedBin = ([string]$owned.install_root).TrimEnd('\', '/') + '\bin'
    foreach ($pair in @(
        @([string]$owned.bin_dir, $expectedBin),
        @([string]$owned.launcher, ($expectedBin + '\goalrouter.ps1')),
        @([string]$owned.cmd, ($expectedBin + '\goalrouter.cmd')),
        @([string]$owned.installer, ($expectedBin + '\install.ps1')),
        @([string]$owned.uninstaller, ($expectedBin + '\uninstall.ps1')),
        @([string]$Manifest.path_ownership.owned_value, $expectedBin)
    )) {
        if (-not (Test-GoalRouterWindowsPathEquivalent -First ([string]$pair[0]) -Second ([string]$pair[1]))) { throw 'trusted install control path relationships are invalid' }
    }
    $configPath = [string]$owned.config_file
    $separatorIndex = [Math]::Max($configPath.LastIndexOf('\'), $configPath.LastIndexOf('/'))
    if ($separatorIndex -lt 3 -or -not (Test-GoalRouterWindowsPathEquivalent -First $configPath.Substring(0, $separatorIndex) -Second ([string]$owned.config_dir))) { throw 'trusted install control config relationship is invalid' }
    if (-not [string]::IsNullOrEmpty($TrustedJson) -and (ConvertTo-GoalRouterCanonicalInstalledManifestJson $Manifest) -cne $TrustedJson) { throw 'trusted install control is not canonical' }
    if ([int]$Manifest.manifest_version -ne 1 -or [int]$Manifest.protocol_version -ne 1 -or [string]$Manifest.version -cnotmatch '\A[0-9]+\.[0-9]+\.[0-9]+\z' -or [string]$Manifest.launcher_version -cne [string]$Manifest.version) { throw 'trusted install control version or protocol is invalid' }
    $trustedImageReference = [string]$Manifest.image_reference
    if (-not (Test-GoalRouterNamedImage $trustedImageReference) -or $trustedImageReference.LastIndexOf(':') -gt $trustedImageReference.LastIndexOf('/') -or -not (Test-GoalRouterDigest ([string]$Manifest.image_digest)) -or [string]$Manifest.image_platform -cnotin @('linux/amd64', 'linux/arm64') -or -not (Test-GoalRouterSingleLine ([string]$Manifest.source_revision)) -or -not (Test-GoalRouterWslDistribution ([string]$Manifest.wsl_distribution))) { throw 'trusted install control runtime authority is invalid' }
    [void](Assert-GoalRouterTrustedReleaseUri -Uri ([string]$Manifest.release_base))
    foreach ($name in @('installer_added', 'update_enabled')) { if ($Manifest.path_ownership.$name -isnot [bool]) { throw 'trusted PATH ownership flag is invalid' } }
    if ([string]$Manifest.path_ownership.before_state -cnotin @('absent', 'empty', 'populated')) { throw 'trusted PATH ownership state is invalid' }
    $beforeState = [string]$Manifest.path_ownership.before_state
    $afterKindPresent = $null -ne $Manifest.path_ownership.after_value_kind
    $afterHashPresent = $null -ne $Manifest.path_ownership.after_sha256
    $installerAdded = [bool]$Manifest.path_ownership.installer_added
    $updateEnabled = [bool]$Manifest.path_ownership.update_enabled
    if (($beforeState -ceq 'absent' -and $null -ne $Manifest.path_ownership.before_value_kind) -or ($beforeState -cne 'absent' -and [string]$Manifest.path_ownership.before_value_kind -cnotin @('String', 'ExpandString')) -or ($afterKindPresent -ne $afterHashPresent) -or ($afterKindPresent -and [string]$Manifest.path_ownership.after_value_kind -cnotin @('String', 'ExpandString')) -or ($afterHashPresent -and [string]$Manifest.path_ownership.after_sha256 -cnotmatch '\A[0-9a-f]{64}\z') -or ($installerAdded -and (-not $updateEnabled -or -not $afterHashPresent)) -or (-not $afterHashPresent -and ($beforeState -cne 'absent' -or $installerAdded -or $updateEnabled)) -or (-not $installerAdded -and $updateEnabled -and $beforeState -cne 'populated') -or (-not $installerAdded -and -not $updateEnabled -and $afterHashPresent -and $beforeState -ceq 'absent') -or ($installerAdded -and $beforeState -ceq 'absent' -and [string]$Manifest.path_ownership.after_value_kind -cne 'String') -or ($beforeState -cne 'absent' -and $afterKindPresent -and [string]$Manifest.path_ownership.after_value_kind -cne [string]$Manifest.path_ownership.before_value_kind)) { throw 'trusted PATH ownership semantics are invalid' }
}

function New-GoalRouterMaintenanceInvocation {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('update', 'uninstall')][string]$Command,
        [string[]]$CommandArguments,
        [Parameter(Mandatory = $true)]$Manifest,
        [Parameter(Mandatory = $true)][string]$PhysicalLauncherPath,
        [ValidateSet('existing-session', 'api-key')][string]$AuthMode = 'existing-session'
    )
    $owned = $Manifest.owned
    if (-not (Test-GoalRouterWindowsPathEquivalent -First $PhysicalLauncherPath -Second ([string]$owned.launcher))) { throw 'physical launcher path does not match trusted install control' }
    if ($Command -ceq 'update') {
        if ($CommandArguments.Count -gt 1) { throw 'invalid update arguments' }
        $selectedVersion = if ($CommandArguments.Count -eq 1) { [string]$CommandArguments[0] } else { 'latest' }
        if ($selectedVersion -cne 'latest' -and $selectedVersion -cnotmatch '\A[0-9]+\.[0-9]+\.[0-9]+\z') { throw 'invalid update version' }
        $releaseBase = [string]$Manifest.release_base
        $canonicalRelease = $releaseBase -cmatch '\Ahttps://github\.com/vparla/GoalRouter/releases/download/v[0-9]+\.[0-9]+\.[0-9]+\z'
        if ($canonicalRelease -and $selectedVersion -cne 'latest') {
            $releaseBase = "https://github.com/vparla/GoalRouter/releases/download/v$selectedVersion"
        }
        $image = [string]$Manifest.image_reference + ':' + $selectedVersion
        $updateArguments = @(
            '-Version', $selectedVersion,
            '-InstallRoot', [string]$owned.install_root,
            '-BinDir', [string]$owned.bin_dir,
            '-ConfigFile', [string]$owned.config_file,
            '-StateDir', [string]$owned.state_dir,
            '-CodexHome', [string]$owned.codex_home,
            '-WslDistribution', [string]$Manifest.wsl_distribution
        )
        if (-not ($canonicalRelease -and $selectedVersion -ceq 'latest')) { $updateArguments += @('-ReleaseBase', $releaseBase) }
        $updateArguments += @('-Image', $image, '-AuthMode', $AuthMode, '-Yes')
        if ($releaseBase -cmatch '\Ahttp://(?:127\.0\.0\.1|localhost|\[?::1\]?)') { $updateArguments += '-AllowLoopbackHttp' }
        if ($Manifest.path_ownership.PSObject.Properties.Name -contains 'update_enabled' -and -not [bool]$Manifest.path_ownership.update_enabled) { $updateArguments += '-NoPathUpdate' }
        elseif (-not [bool]$Manifest.path_ownership.installer_added) { $updateArguments += '-NoPathUpdate' }
        return [pscustomobject]@{
            FilePath = [string]$owned.installer
            Arguments = $updateArguments
        }
    }
    $seenPurge = $false
    $seenYes = $false
    foreach ($argument in $CommandArguments) {
        if ($argument -ceq '-Purge') {
            if ($seenPurge) { throw 'duplicate uninstall option: -Purge' }
            $seenPurge = $true
        } elseif ($argument -ceq '-Yes') {
            if ($seenYes) { throw 'duplicate uninstall option: -Yes' }
            $seenYes = $true
        } else { throw "invalid uninstall argument: $argument" }
    }
    $uninstallArguments = @('-InstallRoot', [string]$owned.install_root)
    if ($seenPurge) { $uninstallArguments += '-Purge' }
    if ($seenYes) { $uninstallArguments += '-Yes' }
    return [pscustomobject]@{ FilePath = [string]$owned.uninstaller; Arguments = $uninstallArguments }
}

function Get-GoalRouterPhysicalInstallControl {
    param([string]$PhysicalLauncherPath = $PSCommandPath)
    if ([string]::IsNullOrEmpty($PhysicalLauncherPath) -or -not (Test-Path -LiteralPath $PhysicalLauncherPath -PathType Leaf)) { return $null }
    $resolvedLauncher = Assert-GoalRouterTrustedPhysicalLeaf -Path $PhysicalLauncherPath -Label 'installed launcher'
    $binDirectory = Split-Path -Parent $resolvedLauncher
    if ((Split-Path -Leaf $binDirectory) -ine 'bin' -or (Split-Path -Leaf $resolvedLauncher) -ine 'goalrouter.ps1') { return $null }
    $installRoot = Split-Path -Parent $binDirectory
    [void](Assert-GoalRouterTrustedPhysicalDirectory -Path $installRoot -Label 'install root')
    [void](Assert-GoalRouterTrustedPhysicalDirectory -Path $binDirectory -Label 'bin directory')
    $manifestPath = Join-Path $installRoot 'install.json'
    $checksumPath = Join-Path $installRoot 'install.sha256'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'trusted install control is missing' }
    if (-not (Test-Path -LiteralPath $checksumPath -PathType Leaf)) { throw 'trusted install control checksum is missing' }
    [void](Assert-GoalRouterTrustedPhysicalLeaf -Path $manifestPath -Label 'trusted install control')
    [void](Assert-GoalRouterTrustedPhysicalLeaf -Path $checksumPath -Label 'trusted install control checksum')
    $trustedJson = Read-GoalRouterTrustedUtf8Text -Path $manifestPath -Label 'trusted install control'
    $checksumText = Read-GoalRouterTrustedUtf8Text -Path $checksumPath -Label 'trusted install control checksum'
    if ($checksumText -cnotmatch '\A[0-9a-f]{64}\n\z') { throw 'trusted install control checksum is invalid' }
    $expectedHash = $checksumText.Substring(0, 64)
    if ($expectedHash -cnotmatch '\A[0-9a-f]{64}\z' -or (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant() -cne $expectedHash) { throw 'trusted install control checksum is invalid' }
    if (-not (Test-GoalRouterPathText $trustedJson)) { throw 'trusted install control contains invalid bytes' }
    try { $manifest = $trustedJson | ConvertFrom-Json -ErrorAction Stop }
    catch { throw 'trusted install control is corrupt' }
    Assert-GoalRouterInstalledManifestSchema -Manifest $manifest -TrustedJson $trustedJson
    if ([int]$manifest.manifest_version -ne 1 -or [int]$manifest.protocol_version -ne 1) { throw 'trusted install control protocol is invalid' }
    if (-not (Test-GoalRouterWindowsPathEquivalent -First ([string]$manifest.owned.install_root) -Second $installRoot) -or -not (Test-GoalRouterWindowsPathEquivalent -First ([string]$manifest.owned.bin_dir) -Second $binDirectory) -or -not (Test-GoalRouterWindowsPathEquivalent -First ([string]$manifest.owned.launcher) -Second $resolvedLauncher)) { throw 'trusted install control does not match the physical launcher' }
    if (-not (Test-GoalRouterWindowsPathEquivalent -First ([string]$manifest.owned.installer) -Second (Join-Path $binDirectory 'install.ps1')) -or -not (Test-GoalRouterWindowsPathEquivalent -First ([string]$manifest.owned.uninstaller) -Second (Join-Path $binDirectory 'uninstall.ps1'))) { throw 'trusted lifecycle paths are not physical launcher siblings' }
    [void](Assert-GoalRouterTrustedPhysicalLeaf -Path ([string]$manifest.owned.installer) -Label 'trusted installer')
    [void](Assert-GoalRouterTrustedPhysicalLeaf -Path ([string]$manifest.owned.uninstaller) -Label 'trusted uninstaller')
    if (-not (Test-GoalRouterWslDistribution ([string]$manifest.wsl_distribution)) -or -not (Test-GoalRouterNamedImage ([string]$manifest.image_reference)) -or -not (Test-GoalRouterDigest ([string]$manifest.image_digest)) -or -not (Test-GoalRouterSingleLine ([string]$manifest.source_revision))) { throw 'trusted install control contains invalid runtime authority' }
    [void](Assert-GoalRouterTrustedPhysicalDirectory -Path ([string]$manifest.owned.config_dir) -Label 'trusted config directory')
    [void](Assert-GoalRouterTrustedPhysicalLeaf -Path ([string]$manifest.owned.config_file) -Label 'trusted config file')
    [void](Assert-GoalRouterTrustedPhysicalDirectory -Path ([string]$manifest.owned.state_dir) -Label 'trusted state directory')
    $stateManifestPath = Join-Path ([string]$manifest.owned.state_dir) 'install.json'
    $stateChecksumPath = Join-Path ([string]$manifest.owned.state_dir) 'install.sha256'
    if (-not (Test-Path -LiteralPath $stateManifestPath -PathType Leaf)) { throw 'runtime state parity manifest is missing' }
    [void](Assert-GoalRouterTrustedPhysicalLeaf -Path $stateManifestPath -Label 'runtime state parity manifest')
    [void](Assert-GoalRouterTrustedPhysicalLeaf -Path $stateChecksumPath -Label 'runtime state parity checksum')
    $stateJson = Read-GoalRouterTrustedUtf8Text -Path $stateManifestPath -Label 'runtime state parity manifest'
    Assert-GoalRouterTrustedStateParity -TrustedJson $trustedJson -StateJson $stateJson
    $stateChecksumText = Read-GoalRouterTrustedUtf8Text -Path $stateChecksumPath -Label 'runtime state parity checksum'
    if ($stateChecksumText -cne $checksumText) { throw 'runtime state control checksum parity is invalid' }
    return $manifest
}

function Invoke-GoalRouterInstalledLifecycle {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('update', 'uninstall')][string]$Command,
        [string[]]$CommandArguments,
        [Parameter(Mandatory = $true)]$Manifest,
        [Parameter(Mandatory = $true)][string]$PhysicalLauncherPath,
        [ValidateSet('existing-session', 'api-key')][string]$AuthMode = 'existing-session'
    )
    $invocation = New-GoalRouterMaintenanceInvocation -Command $Command -CommandArguments $CommandArguments -Manifest $Manifest -PhysicalLauncherPath $PhysicalLauncherPath -AuthMode $AuthMode
    $arguments = @('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', [string]$invocation.FilePath) + @($invocation.Arguments)
    $result = Invoke-GoalRouterNative -FilePath 'powershell.exe' -Arguments $arguments
    return [int]$result.ExitCode
}

function ConvertFrom-GoalRouterDoctorArguments {
    param([string[]]$Arguments)
    $seenSkipAccount = $false
    foreach ($argument in $Arguments) {
        if ($argument -cne '-SkipAccount') { throw "invalid doctor argument: $argument" }
        if ($seenSkipAccount) { throw 'duplicate doctor option: -SkipAccount' }
        $seenSkipAccount = $true
    }
    return $seenSkipAccount
}

function Invoke-GoalRouterStateWriteProbe {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [scriptblock]$WriteProbe = { param([string]$Path); $stream = [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None); try { $stream.WriteByte(0) } finally { $stream.Dispose() } },
        [scriptblock]$RemoveProbe = { param([string]$Path); if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -ErrorAction Stop } }
    )
    $probeFailure = $null
    $cleanupFailure = $null
    try { & $WriteProbe -Path $Path }
    catch { $probeFailure = $_ }
    finally {
        try { & $RemoveProbe -Path $Path }
        catch { $cleanupFailure = $_ }
    }
    if ($null -ne $probeFailure -and $null -ne $cleanupFailure) { throw "$($probeFailure.Exception.Message); doctor state probe cleanup failed: $($cleanupFailure.Exception.Message)" }
    if ($null -ne $probeFailure) { throw $probeFailure }
    if ($null -ne $cleanupFailure) { throw $cleanupFailure }
}

function Invoke-GoalRouterWithApiKeyEnvironment {
    param([Parameter(Mandatory = $true)][scriptblock]$Action)
    if ([string]::IsNullOrEmpty($env:OPENAI_API_KEY)) { throw 'OPENAI_API_KEY is required for api-key mode' }
    $priorWslEnvSnapshot = Get-GoalRouterProcessEnvironmentValue -Name 'WSLENV'
    try {
        if ([string]::IsNullOrEmpty($priorWslEnvSnapshot.Value)) { Set-GoalRouterProcessEnvironmentValue -Name 'WSLENV' -Value 'OPENAI_API_KEY/u' }
        elseif ([string]$priorWslEnvSnapshot.Value -cnotmatch '(^|:)OPENAI_API_KEY/u(:|$)') { Set-GoalRouterProcessEnvironmentValue -Name 'WSLENV' -Value ([string]$priorWslEnvSnapshot.Value + ':OPENAI_API_KEY/u') }
        return & $Action
    } finally {
        if ($priorWslEnvSnapshot.Present) { Set-GoalRouterProcessEnvironmentValue -Name 'WSLENV' -Value ([string]$priorWslEnvSnapshot.Value) }
        else { Remove-GoalRouterProcessEnvironmentValue -Name 'WSLENV' }
    }
}

function Invoke-GoalRouterInstalledDoctor {
    param([Parameter(Mandatory = $true)]$Context, [bool]$SkipAccount)
    $doctorAction = {
    if ($Context.AuthMode -ceq 'existing-session') {
        if (-not ($Context.PSObject.Properties.Name -contains 'CodexWindows')) { throw 'doctor: existing-session authentication source is unavailable' }
        [void](Assert-GoalRouterTrustedCodexSessionDirectory -Path ([string]$Context.CodexWindows) -Label 'doctor existing-session Codex home')
        $authFile = Join-Path ([string]$Context.CodexWindows) 'auth.json'
        if (-not (Test-Path -LiteralPath $authFile -PathType Leaf)) { throw 'doctor: existing-session authentication source is unavailable or unsafe' }
    }
    $prefix = @('-d', [string]$Context.Distribution, '--', 'docker')
    foreach ($probe in @(
        @('version'),
        @('image', 'inspect', [string]$Context.Image)
    )) {
        $result = Invoke-GoalRouterNative -FilePath 'wsl.exe' -Arguments ($prefix + $probe) -CaptureOutput $true
        if ([int]$result.ExitCode -ne 0) { return [int]$result.ExitCode }
    }
    if ($Context.PSObject.Properties.Name -contains 'StateWindows') {
        $probePath = Join-Path ([string]$Context.StateWindows) ('.goalrouter-doctor-' + [guid]::NewGuid().ToString('N') + '.tmp')
        Invoke-GoalRouterStateWriteProbe -Path $probePath
    }
    $Context.Json = $false
    $Context.Forwarded = @('config', 'validate')
    $validation = Invoke-GoalRouterNative -FilePath 'wsl.exe' -Arguments ($prefix + (New-GoalRouterDockerArguments -Context $Context)) -CaptureOutput $true
    if ([int]$validation.ExitCode -ne 0) { return [int]$validation.ExitCode }
    if (-not $SkipAccount) {
        $Context.Forwarded = @('models')
        $models = Invoke-GoalRouterNative -FilePath 'wsl.exe' -Arguments ($prefix + (New-GoalRouterDockerArguments -Context $Context)) -CaptureOutput $true
        if ([int]$models.ExitCode -ne 0) { return [int]$models.ExitCode }
    }
    [Console]::Out.WriteLine('doctor: ok')
    return 0
    }.GetNewClosure()
    if ($Context.AuthMode -ceq 'api-key') { return Invoke-GoalRouterWithApiKeyEnvironment -Action $doctorAction }
    return & $doctorAction
}

function New-GoalRouterTrustedVersionRecord {
    param([Parameter(Mandatory = $true)]$Manifest, [Parameter(Mandatory = $true)][string]$RuntimeJson)
    try { $runtime = $RuntimeJson | ConvertFrom-Json -ErrorAction Stop }
    catch { throw 'verified runtime version output is invalid' }
    if ([string]$runtime.version -cne [string]$Manifest.version) { throw 'verified runtime version does not match trusted control' }
    if ([int]$runtime.protocol_version -ne [int]$Manifest.protocol_version) { throw 'verified runtime protocol does not match trusted control' }
    return [pscustomobject][ordered]@{
        launcher_version = [string]$Manifest.launcher_version
        protocol_version = [int]$Manifest.protocol_version
        image_reference = [string]$Manifest.image_reference
        image_digest = [string]$Manifest.image_digest
        source_revision = [string]$Manifest.source_revision
        image_platform = [string]$Manifest.image_platform
        wsl_distribution = [string]$Manifest.wsl_distribution
        runtime = $runtime
    }
}

function Invoke-GoalRouterInstalledVersion {
    param([Parameter(Mandatory = $true)]$Context, [Parameter(Mandatory = $true)]$Manifest, [bool]$AsJson)
    if ($Context.AuthMode -ceq 'existing-session') {
        if (-not ($Context.PSObject.Properties.Name -contains 'CodexWindows')) { throw 'version existing-session Codex home is unavailable' }
        [void](Assert-GoalRouterTrustedCodexSessionDirectory -Path ([string]$Context.CodexWindows) -Label 'version existing-session Codex home')
    }
    $versionAction = {
    $Context.Json = $true
    $Context.Forwarded = @('version')
    $wslArguments = @('-d', [string]$Context.Distribution, '--', 'docker') + (New-GoalRouterDockerArguments -Context $Context)
    $result = Invoke-GoalRouterNative -FilePath 'wsl.exe' -Arguments $wslArguments -CaptureOutput $true
    if ([int]$result.ExitCode -ne 0) { return [int]$result.ExitCode }
    $record = New-GoalRouterTrustedVersionRecord -Manifest $Manifest -RuntimeJson (@($result.Output) -join "`n")
    if ($AsJson) { [Console]::Out.WriteLine(($record | ConvertTo-Json -Compress -Depth 6)) }
    else {
        foreach ($name in @('launcher_version', 'protocol_version', 'image_reference', 'image_digest', 'source_revision', 'image_platform', 'wsl_distribution')) { [Console]::Out.WriteLine("$name=$($record.$name)") }
        [Console]::Out.WriteLine("runtime_version=$($record.runtime.version)")
        [Console]::Out.WriteLine("runtime_protocol_version=$($record.runtime.protocol_version)")
    }
    return 0
    }.GetNewClosure()
    if ($Context.AuthMode -ceq 'api-key') { return Invoke-GoalRouterWithApiKeyEnvironment -Action $versionAction }
    return & $versionAction
}

function Select-GoalRouterImage {
    param([Parameter(Mandatory = $true)]$Parsed, $TrustedInstall = $null, [Parameter(Mandatory = $true)][string]$StateWindows, [bool]$RequireTrustedImage = $false)
    if ($RequireTrustedImage) {
        if ($null -eq $TrustedInstall) { throw 'trusted installation control is required for maintenance image selection' }
        $trustedImage = [string]$TrustedInstall.image_reference + '@' + [string]$TrustedInstall.image_digest
        if ($Parsed.ImageIsExplicit -and [string]$Parsed.Image -cne $trustedImage) { throw 'explicit image does not match trusted installation control' }
        return $trustedImage
    }
    if ($Parsed.ImageIsExplicit) {
        if (-not (Test-GoalRouterImageOverride -Image $Parsed.Image)) { throw 'invalid --image value' }
        return [string]$Parsed.Image
    }
    if ($null -ne $TrustedInstall) { return [string]$TrustedInstall.image_reference + '@' + [string]$TrustedInstall.image_digest }
    $imageReference = Read-GoalRouterMetadata -Path (Join-Path $StateWindows 'image-ref') -Name 'image-ref'
    if ($imageReference.Contains('@') -or -not (Test-GoalRouterNamedImage $imageReference)) { throw 'invalid image reference metadata' }
    $imageDigest = Read-GoalRouterMetadata -Path (Join-Path $StateWindows 'image-digest') -Name 'image-digest'
    if (-not (Test-GoalRouterDigest $imageDigest)) { throw 'invalid image digest metadata' }
    return $imageReference + '@' + $imageDigest
}

function Get-GoalRouterTrustedMaintenanceInputs {
    param([Parameter(Mandatory = $true)]$Parsed, [Parameter(Mandatory = $true)]$TrustedInstall)
    foreach ($candidate in @(
        @{ Explicit = $null -ne $Parsed.Config; Value = $Parsed.Config; Trusted = [string]$TrustedInstall.owned.config_file; Label = 'config' },
        @{ Explicit = $null -ne $Parsed.StateDir; Value = $Parsed.StateDir; Trusted = [string]$TrustedInstall.owned.state_dir; Label = 'state directory' },
        @{ Explicit = $null -ne $Parsed.CodexHome; Value = $Parsed.CodexHome; Trusted = [string]$TrustedInstall.owned.codex_home; Label = 'Codex home' }
    )) {
        if ($candidate.Explicit -and -not (Test-GoalRouterWindowsPathEquivalent -First ([string]$candidate.Value) -Second ([string]$candidate.Trusted))) { throw "explicit $($candidate.Label) does not match trusted installation control" }
    }
    return [pscustomobject]@{ Config = [string]$TrustedInstall.owned.config_file; StateDir = [string]$TrustedInstall.owned.state_dir; CodexHome = [string]$TrustedInstall.owned.codex_home }
}

function New-GoalRouterContext {
    param([Parameter(Mandatory = $true)]$Parsed, $TrustedInstall = $null, [bool]$RequireTrustedMaintenance = $false)
    if ([string]::IsNullOrEmpty($env:APPDATA)) { throw 'APPDATA is required' }
    if ([string]::IsNullOrEmpty($env:LOCALAPPDATA)) { throw 'LOCALAPPDATA is required' }
    if ([string]::IsNullOrEmpty($env:USERPROFILE)) { throw 'USERPROFILE is required' }

    $distribution = if ($null -ne $TrustedInstall) { [string]$TrustedInstall.wsl_distribution } else { Get-GoalRouterInstalledDistribution -LocalAppData $env:LOCALAPPDATA }
    $projectInput = if ($null -ne $Parsed.Project) { $Parsed.Project } else { (Get-Location).Path }
    $maintenanceInputs = if ($RequireTrustedMaintenance) { Get-GoalRouterTrustedMaintenanceInputs -Parsed $Parsed -TrustedInstall $TrustedInstall } else { $null }
    $configInput = if ($null -ne $maintenanceInputs) { $maintenanceInputs.Config } elseif ($null -ne $Parsed.Config) { $Parsed.Config } elseif ($null -ne $TrustedInstall) { [string]$TrustedInstall.owned.config_file } else { Join-Path (Join-Path $env:APPDATA 'GoalRouter') 'task-models.yaml' }
    $stateInput = if ($null -ne $maintenanceInputs) { $maintenanceInputs.StateDir } elseif ($null -ne $Parsed.StateDir) { $Parsed.StateDir } elseif ($null -ne $TrustedInstall) { [string]$TrustedInstall.owned.state_dir } else { Join-Path (Join-Path $env:LOCALAPPDATA 'GoalRouter') 'state' }
    $codexInput = if ($null -ne $maintenanceInputs) { $maintenanceInputs.CodexHome } elseif ($null -ne $Parsed.CodexHome) { $Parsed.CodexHome } elseif ($null -ne $TrustedInstall) { [string]$TrustedInstall.owned.codex_home } else { Join-Path $env:USERPROFILE '.codex' }
    if ($RequireTrustedMaintenance -and $Parsed.Access -cne 'readonly') { throw 'installed doctor and version require readonly access' }

    $projectResolved = Resolve-GoalRouterHostPath -Path $projectInput -Kind Directory -Label project -Distribution $distribution
    $configResolved = Resolve-GoalRouterHostPath -Path $configInput -Kind File -Label config -Distribution $distribution
    $stateResolved = Resolve-GoalRouterHostPath -Path $stateInput -Kind Directory -Label state -Distribution $distribution
    $project = $projectResolved.WslPath
    $config = $configResolved.WslPath
    $state = $stateResolved.WslPath
    $codexHome = $null
    $codexWindows = $null
    if ($Parsed.AuthMode -ceq 'existing-session') {
        $codexResolved = Resolve-GoalRouterHostPath -Path $codexInput -Kind Directory -Label codex -Distribution $distribution
        $codexHome = $codexResolved.WslPath
        $codexWindows = $codexResolved.ProviderPath
    }

    $image = Select-GoalRouterImage -Parsed $Parsed -TrustedInstall $TrustedInstall -StateWindows ([string]$stateResolved.ProviderPath) -RequireTrustedImage $RequireTrustedMaintenance

    return [pscustomobject]@{
        Distribution = $distribution
        Access = if ($RequireTrustedMaintenance) { 'readonly' } else { $Parsed.Access }
        AuthMode = $Parsed.AuthMode
        Json = $Parsed.Json
        Project = $project
        Config = $config
        State = $state
        StateWindows = $stateResolved.ProviderPath
        CodexHome = $codexHome
        CodexWindows = $codexWindows
        Image = $image
        Forwarded = @($Parsed.Forwarded)
    }
}

function New-GoalRouterDockerArguments {
    param([Parameter(Mandatory = $true)]$Context)
    $arguments = [System.Collections.Generic.List[string]]::new()
    $arguments.Add('run')
    $arguments.Add('--rm')
    $arguments.Add('--read-only')
    $arguments.Add('--tmpfs')
    $arguments.Add('/tmp:rw,exec,nosuid,size=1g,mode=1777')
    $arguments.Add('--mount')
    $arguments.Add("type=bind,src=$($Context.State),dst=/state")
    $arguments.Add('--mount')
    $arguments.Add("type=bind,src=$($Context.Config),dst=/config/task-models.yaml,readonly")
    $arguments.Add('--mount')
    $projectMount = "type=bind,src=$($Context.Project),dst=/project"
    if ($Context.Access -ceq 'readonly') { $projectMount += ',readonly' }
    $arguments.Add($projectMount)
    if ($Context.AuthMode -ceq 'existing-session') {
        $arguments.Add('--mount')
        $arguments.Add("type=bind,src=$($Context.CodexHome),dst=/codex-auth,readonly")
    }
    if ($Context.Access -ceq 'docker') {
        $arguments.Add('--volume')
        $arguments.Add('/var/run/docker.sock:/var/run/docker.sock:rw')
    }
    foreach ($environmentValue in @(
        'GOALROUTER_CONFIG=/config/task-models.yaml',
        'GOALROUTER_STATE_PATH=/state',
        "GOALROUTER_AUTH_MODE=$($Context.AuthMode)"
    )) {
        $arguments.Add('--env')
        $arguments.Add($environmentValue)
    }
    if ($Context.AuthMode -ceq 'existing-session') {
        foreach ($environmentValue in @('GOALROUTER_CODEX_HOME=/codex-auth', 'GOALROUTER_CODEX_STAGING_PATH=/tmp/codex-home')) {
            $arguments.Add('--env')
            $arguments.Add($environmentValue)
        }
    } else {
        $arguments.Add('--env')
        $arguments.Add('OPENAI_API_KEY')
    }
    $arguments.Add([string]$Context.Image)
    if ($Context.Json) { $arguments.Add('--json') }
    foreach ($argument in $Context.Forwarded) { $arguments.Add([string]$argument) }
    return $arguments.ToArray()
}

function Invoke-GoalRouterContext {
    param([Parameter(Mandatory = $true)]$Context)
    if ($Context.AuthMode -ceq 'api-key' -and [string]::IsNullOrEmpty($env:OPENAI_API_KEY)) {
        throw 'OPENAI_API_KEY is required for api-key mode'
    }
    $protocolArguments = @(
        '-d', [string]$Context.Distribution, '--', 'docker',
        'run', '--rm', '--read-only', '--tmpfs',
        '/tmp:rw,exec,nosuid,size=64m,mode=1777',
        [string]$Context.Image, '--json', 'version'
    )
    $protocolResult = Invoke-GoalRouterNative -FilePath 'wsl.exe' -Arguments $protocolArguments -CaptureOutput $true
    if ([int]$protocolResult.ExitCode -ne 0) {
        $protocolFailure = [string](@($protocolResult.Output) -join [Environment]::NewLine)
        if ([string]::IsNullOrEmpty($protocolFailure)) { $protocolFailure = 'registry image metadata query failed before application initialization' }
        throw $protocolFailure
    }
    try { $runtimeMetadata = ([string]$protocolResult.Output) | ConvertFrom-Json }
    catch { throw 'runtime metadata is missing protocol_version' }
    if ($runtimeMetadata.PSObject.Properties.Name -cnotcontains 'protocol_version') { throw 'runtime metadata is missing protocol_version' }
    $runtimeProtocol = [int]$runtimeMetadata.protocol_version
    if ($runtimeProtocol -ne 1) { throw "Launcher protocol 1 cannot run image protocol $runtimeProtocol." }
    $dockerArguments = New-GoalRouterDockerArguments -Context $Context
    $wslArguments = @('-d', [string]$Context.Distribution, '--', 'docker') + $dockerArguments
    $runtimeAction = {
        $captureOutput = [bool]$Context.Json
        $nativeResult = Invoke-GoalRouterNative -FilePath 'wsl.exe' -Arguments $wslArguments -CaptureOutput $captureOutput
        if ($captureOutput -and [int]$nativeResult.ExitCode -ne 0) {
            $nativeFailure = [string](@($nativeResult.Output) -join [Environment]::NewLine)
            if ([string]::IsNullOrEmpty($nativeFailure)) { $nativeFailure = 'application runtime failed' }
            throw $nativeFailure
        }
        if ($captureOutput) {
            foreach ($line in @($nativeResult.Output)) { [Console]::Out.WriteLine([string]$line) }
        }
        return [int]$nativeResult.ExitCode
    }.GetNewClosure()
    if ($Context.AuthMode -cne 'api-key') {
        return & $runtimeAction
    }

    return Invoke-GoalRouterWithApiKeyEnvironment -Action $runtimeAction
}

function Get-GoalRouterFailureCode {
    param([Parameter(Mandatory = $true)][string]$Message)
    if ($Message -cmatch '\ALauncher protocol 1 cannot run image protocol [0-9]+\.\z') { return 'launcher_protocol_mismatch' }
    $normalized = $Message.ToLowerInvariant()
    if ($normalized -match 'authentication|codex|openai_api_key') { return 'authentication' }
    if ($normalized -match 'registry|manifest|image|digest|pull access denied') { return 'registry' }
    if ($normalized -match 'mount|bind source|source path') { return 'mount' }
    if ($normalized -match 'writable|permission|denied|read-only|readonly|owner|acl') { return 'permission' }
    if ($normalized -match 'configuration|config|schema|yaml') { return 'configuration' }
    if ($normalized -match 'docker|wsl|appdata|localappdata|userprofile|required|not found') { return 'prerequisite' }
    return 'application'
}

function ConvertTo-GoalRouterFailureRecord {
    param([Parameter(Mandatory = $true)][string]$Message)
    $failureCode = Get-GoalRouterFailureCode -Message $Message
    $publicMessage = if ($failureCode -ceq 'launcher_protocol_mismatch') {
        $Message
    } else {
        "GoalRouter launcher failed in the $failureCode category."
    }
    return [pscustomobject][ordered]@{
        status = 'error'
        code = $failureCode
        message = $publicMessage
    }
}

function Test-GoalRouterJsonRequested {
    param([string[]]$Arguments)
    $index = 0
    $valueOptions = @('--project', '--access', '--config', '--state-dir', '--codex-home', '--image', '--auth-mode')
    while ($index -lt $Arguments.Count) {
        $argument = [string]$Arguments[$index]
        if ($argument -ceq '--json') { return $true }
        if ($argument -cin $valueOptions) { $index += 2; continue }
        if ($argument -cmatch '\A--') { $index++; continue }
        break
    }
    return $false
}

function Invoke-GoalRouterLauncher {
    param([string[]]$Arguments)
    $parsed = ConvertFrom-GoalRouterArguments -Arguments $Arguments
    if ($parsed.Help) {
        [Console]::Out.WriteLine((Get-GoalRouterHelp))
        return 0
    }
    if ($parsed.AuthMode -ceq 'api-key' -and [string]::IsNullOrEmpty($env:OPENAI_API_KEY)) {
        throw 'OPENAI_API_KEY is required for api-key mode'
    }
    $trustedInstall = Get-GoalRouterPhysicalInstallControl
    $maintenanceCommand = $null
    $installedDoctorSkipAccount = $false
    if ($parsed.Forwarded.Count -gt 0) {
        $maintenanceCommand = [string]$parsed.Forwarded[0]
        if ($maintenanceCommand -ceq 'update' -or $maintenanceCommand -ceq 'uninstall') {
            if ($null -eq $trustedInstall) { throw "$maintenanceCommand requires a trusted installed launcher" }
            $maintenanceArguments = if ($parsed.Forwarded.Count -gt 1) { @($parsed.Forwarded[1..($parsed.Forwarded.Count - 1)]) } else { @() }
            return Invoke-GoalRouterInstalledLifecycle -Command $maintenanceCommand -CommandArguments $maintenanceArguments -Manifest $trustedInstall -PhysicalLauncherPath $PSCommandPath -AuthMode $parsed.AuthMode
        }
        if ($maintenanceCommand -ceq 'doctor' -and $null -ne $trustedInstall) {
            $doctorArguments = if ($parsed.Forwarded.Count -gt 1) { @($parsed.Forwarded[1..($parsed.Forwarded.Count - 1)]) } else { @() }
            $installedDoctorSkipAccount = ConvertFrom-GoalRouterDoctorArguments -Arguments $doctorArguments
            $parsed.Forwarded = @()
        }
    }
    $requiresTrustedMaintenance = $null -ne $trustedInstall -and ($maintenanceCommand -ceq 'doctor' -or $maintenanceCommand -ceq 'version')
    $context = New-GoalRouterContext -Parsed $parsed -TrustedInstall $trustedInstall -RequireTrustedMaintenance $requiresTrustedMaintenance
    if ($null -ne $trustedInstall -and $maintenanceCommand -ceq 'doctor') {
        return Invoke-GoalRouterInstalledDoctor -Context $context -SkipAccount $installedDoctorSkipAccount
    }
    if ($null -ne $trustedInstall -and $parsed.Forwarded.Count -gt 0 -and [string]$parsed.Forwarded[0] -ceq 'version') {
        if ($parsed.Forwarded.Count -ne 1) { throw 'installed version accepts no command arguments' }
        return Invoke-GoalRouterInstalledVersion -Context $context -Manifest $trustedInstall -AsJson ([bool]$parsed.Json)
    }
    return Invoke-GoalRouterContext -Context $context
}

$goalRouterIsDotSourced = $MyInvocation.InvocationName -ceq '.'
if ($goalRouterIsDotSourced -and $env:GOALROUTER_LAUNCHER_TEST_MODE -ceq '1') { return }
if ($goalRouterIsDotSourced) { throw 'dot-sourcing goalrouter.ps1 requires GOALROUTER_LAUNCHER_TEST_MODE=1' }

try {
    $launcherExitCode = Invoke-GoalRouterLauncher -Arguments $ArgumentList
    exit $launcherExitCode
} catch {
    $failureMessage = [string]$_.Exception.Message
    $jsonRequested = Test-GoalRouterJsonRequested -Arguments $ArgumentList
    if ($jsonRequested) {
        $failureRecord = ConvertTo-GoalRouterFailureRecord -Message $failureMessage
        [Console]::Out.WriteLine(($failureRecord | ConvertTo-Json -Depth 3))
    } else {
        [Console]::Error.WriteLine("goalrouter launcher: $failureMessage")
    }
    exit 1
}
