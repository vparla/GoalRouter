<!-- SPDX-License-Identifier: MIT -->
<!-- File: docs/quickstart.md -->
<!-- Purpose: First safe GoalRouter workflow on Windows and POSIX -->

# Quickstart

This quickstart begins with read-only commands. The installed native launcher starts the
pinned container and supplies only the selected mounts. No host Python is involved.

## Windows read-only path

Replace `C:\src\example` with an existing local project:

```powershell
goalrouter doctor
goalrouter --project C:\src\example --access readonly config validate
goalrouter --project C:\src\example --access readonly models
goalrouter --project C:\src\example --access readonly --json route --project /project --task repository-search --prompt 'Locate the test boundary'
```

## POSIX read-only path

```sh
cd /path/to/example
goalrouter doctor
goalrouter --project "$PWD" --access readonly config validate
goalrouter --project "$PWD" --access readonly models
goalrouter --project "$PWD" --access readonly --json route --project /project --task repository-search --prompt 'Locate the test boundary'
```

The first `--project` appears before the command and is a launcher option containing the
host path. Commands that inspect or run a project also take application option
`--project /project` after the command. `/project` is the stable container path.

`route` validates the configured model inventory and returns one deterministic route. It
does not create state or run a Codex turn. Inspect `task`, `model`, `reasoning_effort`,
`sandbox`, `approval`, `timeout_seconds`, and `reason` in the JSON response.

## Plan without execution

Planning calls a Codex turn, may consume included plan allowance or API usage according to
the selected sign-in method, and writes resumable state. It does not dispatch worker items.

```text
goalrouter --project /path/to/example --access readonly plan --project /project --objective "Describe and verify the bounded change" --run-id first-plan
goalrouter --access readonly status first-plan
goalrouter --access readonly report first-plan
```

On Windows, replace `/path/to/example` with a local drive path. The state and config paths
come from trusted install metadata, so later status/report commands do not need a project
override.

## Run one bounded read-only task

```text
goalrouter --project /path/to/example --access readonly run --project /project --task repository-search --prompt "Find the implementation and its tests" --run-id first-run
goalrouter --access readonly status first-run
goalrouter --access readonly report first-run
```

Choose `--access write` only for a task whose configured sandbox is `workspace-write` and
whose intended changes you authorize. Choose `--access docker` only when the task must
control the daemon. Launcher access, route sandbox, and approval all apply; selecting a
broader mount does not bypass the other controls.

Read [CLI reference](cli.md) before compound objectives and [Operations](operations.md)
before approval or resume.
