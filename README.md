<!-- SPDX-License-Identifier: MIT -->
<!-- File: README.md -->
<!-- Purpose: Public GoalRouter overview, installation, quickstart, and documentation map -->

# GoalRouter

GoalRouter 1.0.4 is a local controller for routing engineering work through explicit,
repository-neutral Codex policy. It inspects a target repository without executing its
code, validates the signed-in account's model inventory, decomposes objectives into a
dependency graph, runs bounded work with approval and authority controls, and stores
resumable evidence locally.

The canonical repository is `vparla/GoalRouter`; the runtime image is
`ghcr.io/vparla/goalrouter`. GoalRouter installs a native `goalrouter` launcher, but the
application itself always runs in its pinned Python 3.14 container. Windows uses Windows
PowerShell and WSL only to route Docker. POSIX hosts route Docker directly. Host Python is
neither installed nor used.

## Requirements

- Docker Engine or Docker Desktop with a working daemon.
- Windows 10 build 19045 or newer, Windows PowerShell 5.1 or newer, WSL 2.2.3 or newer,
  and the Ubuntu WSL distribution for the default Windows install.
- A Linux or macOS POSIX shell for the Unix installer.
- An `amd64` or `arm64` host.
- A local Codex sign-in. The default `existing-session` mode reuses that state read-only
  and does not require an API key.

The checksummed release manifest is authoritative for minimum host versions.

## Inspect, download, verify, and install

The installer independently validates the release checksum set, release manifest,
archive shape, runtime image digest, launcher protocol, configuration template, and
installed doctor check. Inspect the downloaded installer yourself before executing it.

### Windows

Run in Windows PowerShell:

```powershell
$Version = '1.0.4'
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

`Expand-Archive -WhatIf` is an inspection preview; the installer downloads and validates
the canonical archive itself. Restart the terminal if the new per-user PATH entry is not
visible immediately.

### Linux

Run in a Linux POSIX shell with `sha256sum`:

```sh
version=1.0.4
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
./install.sh --version 1.0.4 --yes
```

### macOS

Run in a macOS POSIX shell with `shasum`:

```sh
version=1.0.4
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
./install.sh --version 1.0.4 --yes
```

On either platform, add `$HOME/.local/bin` to PATH if the installer prints that
instruction.

See [Installation](docs/installation.md) for defaults, custom paths, non-interactive
operation, and installer safety behavior.

## Authentication and access summary

OpenAI Codex supports ChatGPT sign-in and API-key sign-in for local clients. GoalRouter's
default `existing-session` mode mounts the configured Codex home read-only and stages only
the supported session files in container temporary storage. It never silently changes
authentication mode. Explicit `api-key` mode reads `OPENAI_API_KEY` from the launching
process environment only.

Project authority is separate from authentication:

| Access | Project mount | Docker socket | Use |
|---|---|---|---|
| `readonly` | read-only | absent | Inspect, validate, route, plan, status, report, and read-only work. |
| `write` | read-write | absent | Approved changes within the target project. |
| `docker` | read-write | present | Explicit work that must control the Docker daemon. |

The state directory is writable for checkpoints in all three modes. Start with
`readonly`; broader launcher access does not override task sandbox or approval policy.

Plan and run-creation workflows accept repository instructions only as regular files
reached without symbolic links. Those workflows reject an unsafe instruction before their
model inventory, planner, or worker SDK call. Route previews and standalone model inventory
do not consume repository instructions. Mutating run commands are also process-exclusive:
a second mutator of the same run exits `15`, while a second writer for the same physical
project exits `14`. These leases are nonblocking, recover when the owning process exits,
and do not create a lockfile in the target project. The `status` command remains an
unleased, non-mutating snapshot.

## Five-minute read-only quickstart

These commands use the installed launcher and do not dispatch a Codex work item. Replace
the example path with an existing local project. The launcher-side `--project` selects the
host mount; the route command's `--project /project` names that mount inside the runtime.

### Windows

```powershell
goalrouter doctor
goalrouter --project C:\src\example --access readonly config validate
goalrouter --project C:\src\example --access readonly models
goalrouter --project C:\src\example --access readonly route --project /project --task repository-search --prompt 'Locate the test boundary'
```

### POSIX

```sh
cd /path/to/example
goalrouter doctor
goalrouter --project "$PWD" --access readonly config validate
goalrouter --project "$PWD" --access readonly models
goalrouter --project "$PWD" --access readonly route --project /project --task repository-search --prompt 'Locate the test boundary'
```

`doctor` validates the installation, daemon, image, configuration, mounts, writable state,
and—unless skipped—the non-billable model inventory. `route` reports model, reasoning
effort, sandbox, approval, timeout, escalation, and match reason without running an agent
turn. Continue with the full [Quickstart](docs/quickstart.md).

## Documentation

- [Installation](docs/installation.md) — Windows, Linux, and macOS installation guidance.
- [Quickstart](docs/quickstart.md) — first read-only checks, a bounded run, and reports.
- [CLI reference](docs/cli.md) — native launcher options before all application commands.
- [Configuration](docs/configuration.md) — routing and planner schemas.
- [Authentication](docs/authentication.md) — ChatGPT sign-in reuse and explicit key mode.
- [Operations](docs/operations.md) — routine routing, run, approval, resume, and recovery.
- [Security](docs/security.md) — trust boundaries, mounts, credentials, and approvals.
- [Upgrading](docs/upgrading.md) — version updates and preservation behavior.
- [Uninstalling](docs/uninstalling.md) — preserve and exact purge modes.
- [Troubleshooting](docs/troubleshooting.md) — launcher categories and application exits.
- [Architecture](docs/architecture.md) — launcher protocol, request flow, and authority matrix.
- [Development](docs/development.md) — Docker-only contributor lifecycle.
- [Releasing](docs/releasing.md) — version synchronization and protected publication.
- [Testing](docs/testing.md) — deterministic gates and explicit live profiles.
- [Validation projects](docs/validation-projects.md) — repository-neutral validation.
- [Contributing](CONTRIBUTING.md), [Security policy](SECURITY.md), and
  [Changelog](CHANGELOG.md) — project participation and release history.

GoalRouter is licensed under the [MIT License](LICENSE).
