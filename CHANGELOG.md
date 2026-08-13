<!-- SPDX-License-Identifier: MIT -->
<!-- File: CHANGELOG.md -->
<!-- Purpose: Public GoalRouter release history -->

# Changelog

All notable changes to GoalRouter are recorded here.

## [Unreleased]

### Security

- Make plan and run-creation workflows reject linked and non-regular repository instruction
  files before those workflows perform model inventory, planning, dispatch, or state
  creation.
- Serialize same-run mutators and same-project writers across processes and containers with
  nonblocking kernel leases, stable busy exits, and no target-project lock artifact.
- Pin Docker CLI build and smoke helpers by exact version and multi-architecture OCI index
  digest, with repository policy that rejects authority drift.
- Harden repository Git evidence collection against repository and environment-controlled
  Git filesystem monitors, Git hooks, clean/process filters, pagers, prompts, optional index
  writes, lazy object fetching, submodule recursion, malformed metadata, unbounded diagnostics,
  and orphaned subprocesses while retaining linked-worktree, SHA-256, and unusual-filename
  support. Tracked files are compared with descriptor-rooted raw blob hashing in a killable
  GoalRouter helper; normalization or custom filters can conservatively yield false-dirty
  evidence. A command-scoped exact-worktree safe-directory exception supports read-only Docker
  mounts without wildcard trust.

## [1.0.9] - 2026-08-12

### Fixed

- Make the installed Windows uninstaller self-contained so uninstall and purge work in a
  fresh PowerShell process.

## [1.0.8] - 2026-08-12

### Fixed

- Preserve a true null backup argument when atomically replacing existing Windows installer
  files under PowerShell 5.1, while retaining cleanup and underlying failure details.

## [1.0.7] - 2026-08-12

### Fixed

- Validate only the exact Codex session files used by GoalRouter so unrelated Codex App
  cache junctions do not prevent a safe existing-session install.

## [1.0.6] - 2026-08-12

### Fixed

- Execute native Windows PowerShell 5.1 `wslpath` conversion directly through WSL so
  literal drive-path backslashes are preserved.

## [1.0.5] - 2026-08-12

### Fixed

- Parse the complete candidate-version response as one strict JSON document, including
  pretty multi-line output, before accepting its exact version metadata.

## [1.0.4] - 2026-08-12

### Fixed

- Parse Docker image labels as strict quote-free JSON on Windows PowerShell 5.1 before
  accepting the exact image metadata value.

## [1.0.3] - 2026-08-12

### Fixed

- Parse Docker `RepoDigests` as a strict JSON array on Windows PowerShell 5.1 before
  accepting the single repository-qualified release digest.

## [1.0.2] - 2026-08-12

### Fixed

- Stage Windows downloads under the validated LocalAppData root while retaining a
  current-user-owned private work directory and the complete ACL, ancestor, and reparse-point
  safety checks.

## [1.0.1] - 2026-08-12

### Fixed

- Correct Windows PowerShell 5.1 installation and update handling for host-root paths and
  WSL command output so native Windows lifecycle checks remain deterministic.
- Publish patch releases without overwriting immutable image names, advancing `1.0`, `1`,
  and `latest` only after each moving alias is proven to match the prior `1.0.0` digest.

## [1.0.0] - 2026-08-04

### Added

- Native Windows and POSIX launchers, per-user installers, updates, and safe
  preserve-or-purge uninstallers.
- A pinned Python 3.14 runtime image for `linux/amd64` and `linux/arm64`.
- Repository-neutral YAML routing, model inventory validation, structured objective
  decomposition, bounded scheduling, explicit approval, resumable state, and reports.
- Read-only, workspace-write, and Docker-daemon authority profiles.
- Deterministic release assets, checksums, a multi-architecture GHCR publication workflow,
  software bill of materials, provenance, and GitHub attestations.
- Docker-only CI, distribution contracts, and public operational documentation.
