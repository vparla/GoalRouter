<!-- SPDX-License-Identifier: MIT -->
<!-- File: docs/validation-projects.md -->
<!-- Purpose: Repository-neutral validation against representative local projects -->

# Validation projects

GoalRouter has no project profile table. Validation targets demonstrate that one YAML
policy routes different languages and repository instructions without embedding repository
names or paths in configuration. The shipped validation Compose profile exposes neutral
environment names and container mount paths only.

## Safety contract

For each target:

1. Capture exact source-control status, including untracked paths.
2. Mount the target read-only.
3. Run route and inspection validation only; do not dispatch worker turns.
4. Capture status again and require byte-identical output.
5. Review returned task, model, effort, sandbox, approval, timeout, escalation, and reason.

Repository discovery may read applicable instruction files, source-control metadata,
language extensions, and Docker-file presence. It does not execute project code.

## Individual installed-launcher previews

Python target:

```text
goalrouter --project /path/to/python-project --access readonly --json route --project /project --task python-coding --prompt "Validate the generic Python route" --affected-path src/example.py
```

Rust target:

```text
goalrouter --project /path/to/rust-project --access readonly --json route --project /project --task rust-coding --prompt "Validate the generic Rust route" --affected-path src/lib.rs
```

C++ target:

```text
goalrouter --project /path/to/cpp-project --access readonly --json route --project /project --task c-cpp-coding --prompt "Validate the generic C++ route" --affected-path src/example.cpp
```

Also preview repository-search, docker-invoke, unit-test-run, unit-test-debug,
architecture-change, security-sensitive, and release-publish as appropriate. Route preview
does not create state or start a Codex turn, even when the selected policy would require
write access or approval to execute.

## Combined validation profile

Windows PowerShell:

```powershell
wsl.exe -d Ubuntu -- bash -lc "cd /mnt/c/dev/GoalRouter && VALIDATION_PROJECT_ONE=/mnt/c/dev/project-one VALIDATION_PROJECT_TWO=/mnt/c/dev/project-two VALIDATION_PROJECT_THREE=/mnt/c/dev/project-three docker compose -f compose.live.yaml --profile validation run --rm validation"
```

POSIX:

```sh
VALIDATION_PROJECT_ONE=/path/one VALIDATION_PROJECT_TWO=/path/two VALIDATION_PROJECT_THREE=/path/three docker compose -f compose.live.yaml --profile validation run --rm validation
```

All three mounts are read-only and networking is disabled. The profile fails if any target
is unavailable or the generic expected routes diverge. Keep before/after state evidence
outside the targets and preserve any dirty worktree exactly.
