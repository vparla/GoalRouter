<!-- SPDX-License-Identifier: MIT -->
<!-- File: docs/installation.md -->
<!-- Purpose: Native GoalRouter installation on Windows and POSIX hosts -->

# Installation

GoalRouter 1.0.6 installs a small native launcher and keeps the Python application inside
the immutable `ghcr.io/vparla/goalrouter` runtime image. The installer is per-user and
requires a working Docker daemon plus a local Codex sign-in for its default doctor check.
The default `existing-session` mode reuses a Codex home populated by ChatGPT or workspace
SSO sign-in and does not require an API key on Windows, Linux, or macOS.

## Host requirements

The release manifest enforces supported architecture and minimum versions. Version 1.0.6
declares `linux/amd64` and `linux/arm64`; Windows requires build 10.0.19045, Windows
PowerShell 5.1, WSL 2.2.3 with the selected WSL2 distribution, and Docker client and daemon
20.10. POSIX installation requires a supported Linux or macOS shell environment, Docker,
`curl`, `tar`, standard file utilities, and either `sha256sum` or `shasum`.

## Windows: inspect, download, verify, install

Use Windows PowerShell. The canonical installer defaults to the Ubuntu WSL distribution.

```powershell
$Version = '1.0.6'
$Release = "https://github.com/vparla/GoalRouter/releases/download/v$Version"
Invoke-WebRequest "$Release/SHA256SUMS" -OutFile .\SHA256SUMS
Invoke-WebRequest "$Release/install.ps1" -OutFile .\install.ps1
Invoke-WebRequest "$Release/goalrouter-$Version-windows.zip" -OutFile ".\goalrouter-$Version-windows.zip"
Get-Content .\install.ps1
$Checksums = Get-Content .\SHA256SUMS
foreach ($File in @('install.ps1', "goalrouter-$Version-windows.zip")) {
    $Expected = (($Checksums | Where-Object { $_ -match " $([regex]::Escape($File))$" }) -split '\s+')[0]
    $Actual = (Get-FileHash ".\$File" -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Actual -cne $Expected) { throw "$File checksum mismatch" }
}
Expand-Archive -Path ".\goalrouter-$Version-windows.zip" -DestinationPath ".\goalrouter-$Version" -WhatIf
.\install.ps1 -Version $Version -Yes
```

The installation defaults are:

- install root and state: `%LOCALAPPDATA%\GoalRouter`;
- launcher directory: `%LOCALAPPDATA%\GoalRouter\bin`;
- configuration: `%APPDATA%\GoalRouter\task-models.yaml`;
- Codex home: `%USERPROFILE%\.codex`;
- WSL distribution: `Ubuntu`.

The installer records exact ownership and PATH state. It adds only its `bin` directory to
the per-user PATH unless `-NoPathUpdate` is used. A new terminal may be required. Custom
paths use `-InstallRoot`, `-ConfigFile`, `-StateDir`, `-CodexHome`, and
`-WslDistribution`. Windows installation is always explicit and non-interactive: `-Yes`
is mandatory, including for the default command above.

## Linux: inspect, download, verify, install

Use this complete block on Linux with `sha256sum`:

```sh
version=1.0.6
release="https://github.com/vparla/GoalRouter/releases/download/v$version"
curl --fail-with-body --location --proto '=https' --tlsv1.2 "$release/SHA256SUMS" -o SHA256SUMS
curl --fail-with-body --location --proto '=https' --tlsv1.2 "$release/install.sh" -o install.sh
grep ' install.sh$' SHA256SUMS > install.SHA256SUMS
sha256sum -c install.SHA256SUMS
sed -n '1,240p' install.sh
curl --fail-with-body --location --proto '=https' --tlsv1.2 "$release/goalrouter-$version-unix.tar.gz" -o "goalrouter-$version-unix.tar.gz"
grep " goalrouter-$version-unix.tar.gz$" SHA256SUMS > archive.SHA256SUMS
sha256sum -c archive.SHA256SUMS
tar -tzf "goalrouter-$version-unix.tar.gz"
chmod 0700 install.sh
./install.sh --version 1.0.6 --yes
```

## macOS: inspect, download, verify, install

Use this complete block on macOS with `shasum`:

```sh
version=1.0.6
release="https://github.com/vparla/GoalRouter/releases/download/v$version"
curl --fail-with-body --location --proto '=https' --tlsv1.2 "$release/SHA256SUMS" -o SHA256SUMS
curl --fail-with-body --location --proto '=https' --tlsv1.2 "$release/install.sh" -o install.sh
grep ' install.sh$' SHA256SUMS > install.SHA256SUMS
shasum -a 256 -c install.SHA256SUMS
sed -n '1,240p' install.sh
curl --fail-with-body --location --proto '=https' --tlsv1.2 "$release/goalrouter-$version-unix.tar.gz" -o "goalrouter-$version-unix.tar.gz"
grep " goalrouter-$version-unix.tar.gz$" SHA256SUMS > archive.SHA256SUMS
shasum -a 256 -c archive.SHA256SUMS
tar -tzf "goalrouter-$version-unix.tar.gz"
chmod 0700 install.sh
./install.sh --version 1.0.6 --yes
```

The POSIX defaults follow XDG locations:

- launcher: `${XDG_BIN_HOME:-$HOME/.local/bin}/goalrouter`;
- configuration: `${XDG_CONFIG_HOME:-$HOME/.config}/goalrouter/task-models.yaml`;
- state: `${XDG_STATE_HOME:-$HOME/.local/state}/goalrouter`;
- Codex home: `${CODEX_HOME:-$HOME/.codex}`.

The POSIX installer never edits shell startup files. It prints the exact PATH directory
when it is not already present. Custom destinations use `--bin-dir`, `--config-dir`,
`--state-dir`, and `--codex-home`; all must be safe absolute paths. The shown `--yes`
form is the executable non-interactive path; omit it only when deliberately using the
POSIX installer's terminal confirmation prompt.

## What the installer verifies

Both installers download over HTTPS, verify `SHA256SUMS`, require the exact bounded archive
members, validate the canonical release manifest and host minimums, pull the named image,
resolve and record its immutable repository digest, query runtime version/protocol metadata,
emit and validate the candidate configuration, then atomically activate owned files. The
default doctor check validates Docker, the pinned image, config, project mount, writable
state, and a non-billable account model inventory. Any failure rolls back the transaction.

Existing valid configuration is preserved and validated against the candidate image.
`-ResetConfig` or `--reset-config` explicitly replaces it. `-Force` or `--force` is a
repair path that also resets configuration; use it only after verifying every destination.
Skipping doctor is available for controlled recovery, but leaves the installation without
the normal end-to-end acceptance check.

## Confirm the install

```text
goalrouter version
goalrouter doctor
goalrouter --help
```

Continue with [Quickstart](quickstart.md). For sign-in recovery, see
[Authentication](authentication.md).
