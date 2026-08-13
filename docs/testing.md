<!-- SPDX-License-Identifier: MIT -->
<!-- File: docs/testing.md -->
<!-- Purpose: Exact Docker-only GoalRouter verification lifecycle and limitations -->

# Testing

All repository workloads execute in declared containers using Python 3.14 where applicable.
Warnings are errors. On Windows, Windows PowerShell routes Docker through WSL; WSL does not
run Python or repository tools.

## Full deterministic verification

Windows PowerShell:

```powershell
wsl.exe -d Ubuntu -- bash -lc "cd /mnt/c/dev/GoalRouter && docker compose config --quiet"
wsl.exe -d Ubuntu -- bash -lc "cd /mnt/c/dev/GoalRouter && docker compose build --check"
wsl.exe -d Ubuntu -- bash -lc "cd /mnt/c/dev/GoalRouter && docker compose build"
wsl.exe -d Ubuntu -- bash -lc "cd /mnt/c/dev/GoalRouter && docker compose run --rm test"
wsl.exe -d Ubuntu -- bash -lc "cd /mnt/c/dev/GoalRouter && docker compose run --rm lint"
wsl.exe -d Ubuntu -- bash -lc "cd /mnt/c/dev/GoalRouter && docker compose run --rm typecheck"
wsl.exe -d Ubuntu -- bash -lc "cd /mnt/c/dev/GoalRouter && docker compose run --rm package"
wsl.exe -d Ubuntu -- bash -lc "cd /mnt/c/dev/GoalRouter && docker compose run --rm shellcheck"
wsl.exe -d Ubuntu -- bash -lc "cd /mnt/c/dev/GoalRouter && docker compose run --rm powershell-test"
wsl.exe -d Ubuntu -- bash -lc "cd /mnt/c/dev/GoalRouter && docker compose run --rm distribution-test"
```

POSIX:

```sh
docker compose config --quiet
docker compose build --check
docker compose build
docker compose run --rm test
docker compose run --rm lint
docker compose run --rm typecheck
docker compose run --rm package
docker compose run --rm shellcheck
docker compose run --rm powershell-test
docker compose run --rm distribution-test
```

The full `test` service includes unit, integration, architecture, distribution, and
non-live test modules. `distribution-test` is repeated as an explicit release gate. Ruff
runs without cache, strict mypy writes its cache to `/tmp`, and deterministic services have
read-only roots and disabled networking where declared.

## Focused and integration verification

```sh
docker compose build test
docker compose run --rm test python -m pytest tests/architecture -q
docker compose run --rm test python -m pytest tests/distribution/test_documented_commands.py -q
docker compose run --rm distribution-integration
docker compose run --rm runtime-smoke
docker compose run --rm runtime-smoke-interrupt
docker compose run --rm posix-launcher-smoke
docker compose run --rm posix-launcher-smoke-safety
docker compose run --rm posix-installer-smoke
```

The nested-Docker services receive the host daemon socket only to build/start uniquely
labelled disposable fixtures. The runtime launched under read-only or write authority does
not receive the socket; the explicit Docker-authority case does. Each smoke service owns
and cleans its bounded fixtures.

## Interprocess lease evidence

The same-bind writer contention probe is directly verified on Docker Desktop through WSL.
It launches independent Linux containers against one daemon-visible project directory,
proves the first owns the real directory lease, requires the contender to exit `14` before
its SDK marker, terminates the owner, then proves a later contender can acquire ownership.
The probe also requires the target directory to remain empty and audits its labelled
containers and volumes after cleanup.

Unit and integration coverage separately proves same-run contention exits `15`, lease
release after normal and forced owner exit, cancellation-safe descriptor cleanup, no
project artifact, unleased status reads, and planned-state persistence when an approved
writer encounters contention.

Clean-host Linux Docker Engine and macOS Docker Desktop proof remains a release gate; it is
not inferred from the WSL result. The repository supplies Docker-only POSIX installer and
launcher contracts for both platforms, but completion of those native clean-host gates and
external image publication is recorded only when the release validation actually runs.

## Repository Git boundary evidence

Real Git security fixtures prove local, included, global, system, and environment-injected
filesystem monitors cannot execute; inspection leaves the index bytes and metadata
unchanged and creates no `index.lock`. The focused suite also covers disabled Git-hook paths,
submodule non-recursion, linked worktrees, Git ownership refusal, unsafe `.git` entries,
NUL-delimited unusual and non-UTF-8 filenames, bounded diagnostics, timeouts, and repeated
cancellation while the owned child process is killed and reaped. Exact-candidate safe-directory
fixtures cover Docker mount ownership while swap and mismatched-discovery cases prove the trust
does not extend to an unrelated root.

## Immutable helper-image evidence

Distribution policy requires every Docker CLI build/smoke authority to use one exact
numeric version plus its official multi-architecture OCI index digest. Mutation tests
reject tag-only, changed version/digest, extra, relocated, unreachable, and unreviewed
owner-file references. Compose build checks must report no warnings, and smoke services
must still pass with the pinned helper authority.

## PowerShell coverage limitation

The pinned `powershell-test` service runs PowerShell 7.5 on Linux. It dot-sources and
executes the Windows scripts through structural and contract coverage, including
PowerShell syntax, argument parsing, path/ACL abstractions, lifecycle transactions,
launcher parity, and failure cases. Public v1.0.10 additionally passed one native Windows
PowerShell 5.1 lifecycle using checksummed release assets: install, version, configuration
validation, account-skipped doctor, idempotent reinstall, preserve uninstall, reinstall,
and purge uninstall. That evidence proves the native installation lifecycle; it does not
replace the separate readonly-plan and documented-update acceptance requirements.

## Validation profile

Repository-neutral validation is opt-in and read-only:

```sh
VALIDATION_PROJECT_ONE=/path/one VALIDATION_PROJECT_TWO=/path/two VALIDATION_PROJECT_THREE=/path/three docker compose -f compose.live.yaml --profile validation run --rm validation
```

See [Validation projects](validation-projects.md) for pre/post target-state checks.

## Live and non-billable profiles

The installed-launcher inventory profile is opt-in and performs account/model discovery
without starting an agent turn:

```sh
GOALROUTER_CODEX_HOME=/path/to/codex-home docker compose -f compose.live.yaml --profile live-inventory run --rm live-inventory
```

The read-only live turn profile can consume tokens and must be run only with explicit
authorization:

```sh
GOALROUTER_CODEX_HOME=/path/to/codex-home docker compose -f compose.live.yaml --profile live-test run --rm live-test
```

Both mount the source Codex home read-only. Hash credential-bearing files before and after
and require equality. Treat `models_cache.json` separately because another Codex process
may update that noncredential cache concurrently.
