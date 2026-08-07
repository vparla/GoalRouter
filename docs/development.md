<!-- SPDX-License-Identifier: MIT -->
<!-- File: docs/development.md -->
<!-- Purpose: Docker-only GoalRouter contributor workflow -->

# Development

GoalRouter supports Python 3.14 only, and every repository workload runs in a declared
Docker Compose service. The Windows host may inspect and edit files. WSL is limited to
directory and Docker routing. Neither the Windows host nor WSL may run Python, dependency
tools, tests, builds, linters, type checking, package creation, the application CLI, or SDK
checks directly.

## Declared services

- `test`: deterministic pytest in the Python 3.14 test image.
- `lint`: Ruff with cache disabled.
- `typecheck`: strict mypy with cache under container tmpfs.
- `package`: wheel build without dependency resolution or build isolation.
- `shellcheck`: exact POSIX shell contract list.
- `powershell-test`: PowerShell contracts in the pinned PowerShell image.
- `distribution-test`: all deterministic distribution pytest.
- `distribution-integration`: Docker-socket-backed launcher/runtime integration.
- runtime and installer smoke services: bounded nested-Docker lifecycle probes.
- `release-assets`: deterministic local release asset generation.

`compose.live.yaml` contains opt-in validation, non-billable inventory, and live turn
profiles. Do not run live/billable profiles during ordinary development.

## Build the lifecycle

Windows PowerShell:

```powershell
wsl.exe -d Ubuntu -- bash -lc "cd /mnt/c/dev/GoalRouter && docker compose config --quiet"
wsl.exe -d Ubuntu -- bash -lc "cd /mnt/c/dev/GoalRouter && docker compose build --check"
wsl.exe -d Ubuntu -- bash -lc "cd /mnt/c/dev/GoalRouter && docker compose build"
```

POSIX:

```sh
docker compose config --quiet
docker compose build --check
docker compose build
```

BuildKit warnings fail the gate. Do not substitute an ad-hoc image or install a missing
tool on the host.

Every Docker CLI tool-image consumer is pinned by exact version and OCI index digest. The
architecture policy inventories the Dockerfile, Compose services, smoke-command spans, and
distribution assertions that consume that helper, binds each reviewed owner, and rejects
tag-only, changed-digest, additional, or relocated references. Update the authority and all
bound consumers together; never relax the policy to accommodate an unpinned helper.

## Test-first change cycle

1. Identify the smallest public behavior and existing shared capability.
2. Add one focused test against the real Protocol boundary or parser/contract.
3. Rebuild the affected declared image so it contains the new test.
4. Run the focused test and confirm it fails for the missing behavior.
5. Implement the minimum change.
6. Rerun focused and neighboring suites until green.
7. Update all affected durable docs and Mermaid diagrams in the same slice.
8. Run the complete gates in [Testing](testing.md).

Use structural fakes that satisfy Protocols. Keep stable domain values frozen/slotted,
configuration policy in YAML, and SDK imports under `src/goalrouter/sdk`. Public I/O is
async. Do not add polling, unowned background work, silent fallbacks, or compatibility
aliases.

Repository evidence commands are a hostile subprocess boundary. Keep absolute Git
execution, the explicit minimal environment, command-scope execution disables, validated
and pinned worktree/index metadata, optional-lock prohibition, submodule non-recursion,
strict NUL/TAB parsers, bounded diagnostics, and cancellation-safe child reaping together.
Tracked-file comparison must remain outside Git conversion: descriptor-rooted raw reads use
no-follow/nonblocking flags and a killable GoalRouter-owned hash helper. Any change to the Git
allowlist, evidence format, descriptor protocol, or hash-helper lifecycle requires the real
filter sentinel, race, unusual-path, object-format, and blocked-read fixtures plus documentation
contracts.

## Focused examples

Windows PowerShell:

```powershell
wsl.exe -d Ubuntu -- bash -lc "cd /mnt/c/dev/GoalRouter && docker compose build test"
wsl.exe -d Ubuntu -- bash -lc "cd /mnt/c/dev/GoalRouter && docker compose run --rm test python -m pytest tests/unit/test_routing.py -q"
wsl.exe -d Ubuntu -- bash -lc "cd /mnt/c/dev/GoalRouter && docker compose run --rm test python -m goalrouter --help"
```

POSIX:

```sh
docker compose build test
docker compose run --rm test python -m pytest tests/unit/test_routing.py -q
docker compose run --rm test python -m pytest tests/architecture/test_documentation.py tests/distribution/test_documented_commands.py -q
docker compose run --rm test python -m goalrouter --help
```

The Python processes above are inside the declared `test` container.

## Documentation changes

Public docs must stand alone, use installed-launcher examples for normal users, and match
current parser, schema, installer, authority, and release behavior. Contributor commands
remain Docker-only. Update diagrams whenever topology, trust, state, or request flow
changes. Explicitly label partial or structurally tested behavior.
