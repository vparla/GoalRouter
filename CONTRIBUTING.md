<!-- SPDX-License-Identifier: MIT -->
<!-- File: CONTRIBUTING.md -->
<!-- Purpose: Public contribution workflow for GoalRouter -->

# Contributing to GoalRouter

Thank you for improving GoalRouter. Start with the issue or proposed change, keep the
scope bounded, and preserve unrelated local changes. Security vulnerabilities belong in
the private process described in [SECURITY.md](SECURITY.md), not a public issue.

## Development contract

- Python 3.14 is the only supported runtime.
- Docker executes every dependency install, test, lint, type check, package build, CLI
  probe, and SDK check.
- On Windows, Windows PowerShell invokes WSL only to route Docker.
- Warnings are failures.
- New behavior starts with a focused failing test and ends with the complete relevant
  verification gates.
- Extend shared capability and existing Protocol boundaries before adding another path.
- Keep OpenAI Codex SDK imports below `src/goalrouter/sdk`.
- Keep task and execution policy in YAML, never repository identities or paths.
- Do not clean a target worktree or overwrite unrelated changes.

Read [Development](docs/development.md), [Testing](docs/testing.md), and
[Architecture](docs/architecture.md) before changing production behavior.

## Windows verification

Run from Windows PowerShell. WSL performs only directory and Docker routing:

```powershell
wsl.exe -d Ubuntu -- bash -lc "cd /mnt/c/dev/GoalRouter && docker compose build --check"
wsl.exe -d Ubuntu -- bash -lc "cd /mnt/c/dev/GoalRouter && docker compose build"
wsl.exe -d Ubuntu -- bash -lc "cd /mnt/c/dev/GoalRouter && docker compose run --rm test"
wsl.exe -d Ubuntu -- bash -lc "cd /mnt/c/dev/GoalRouter && docker compose run --rm lint"
wsl.exe -d Ubuntu -- bash -lc "cd /mnt/c/dev/GoalRouter && docker compose run --rm typecheck"
wsl.exe -d Ubuntu -- bash -lc "cd /mnt/c/dev/GoalRouter && docker compose run --rm package"
wsl.exe -d Ubuntu -- bash -lc "cd /mnt/c/dev/GoalRouter && docker compose run --rm shellcheck"
wsl.exe -d Ubuntu -- bash -lc "cd /mnt/c/dev/GoalRouter && docker compose run --rm powershell-test"
```

## POSIX verification

```sh
docker compose build --check
docker compose build
docker compose run --rm test
docker compose run --rm lint
docker compose run --rm typecheck
docker compose run --rm package
docker compose run --rm shellcheck
docker compose run --rm powershell-test
```

Use only services declared in `compose.yaml`. Do not invoke live profiles in ordinary
contribution verification.

## Pull requests

Explain the user-visible outcome, list changed files, include the observed failing test
and the final green evidence, identify limitations, and note documentation changes. Keep
commits reviewable and do not combine unrelated cleanup with the change. A pull request
must pass the repository's Docker-only CI before merge.
