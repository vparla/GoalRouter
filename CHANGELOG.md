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
