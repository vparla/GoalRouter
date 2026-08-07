<!-- SPDX-License-Identifier: MIT -->
<!-- File: docs/cli.md -->
<!-- Purpose: Native launcher and containerized application command reference -->

# CLI reference

The installed `goalrouter` command has two parsing layers. Native launcher options must
come before the command. The launcher validates trusted install metadata, resolves host
paths, checks image protocol compatibility, and starts the container. Remaining arguments
are handled by the Python application inside that container.

```text
goalrouter [launcher-options] <command> [application-options]
```

`goalrouter --help` prints the native surface. For application help, place `--help` after
the application command, for example `goalrouter route --help`.

## Native launcher

| Option | Meaning |
|---|---|
| `--project` | Host project directory mounted at `/project`; defaults to the current directory. |
| `--access` | `readonly`, `write`, or `docker`; defaults to `readonly`. |
| `--config` | Host routing YAML file; installed mode accepts the trusted recorded file. |
| `--state-dir` | Host state directory; installed mode accepts the trusted recorded directory. |
| `--codex-home` | Host Codex state directory mounted read-only in `existing-session` mode. |
| `--image` | Explicit image tag or digest; installed behavior differs by host as described below. |
| `--auth-mode` | Exactly `existing-session` or `api-key`; defaults to `existing-session`. |
| `--json` | Request machine-readable application output and sanitized launcher failures. |

Launcher `--project` consumes a host path. The `route`, `plan`, and `run` application
commands separately require `--project /project` after the command because the application
sees the container path.

POSIX installed mode rejects a foreign `--image` override and remains pinned to its
recorded repository digest. Windows ordinary installed runs accept a syntactically valid
explicit `--image` for diagnostic or runtime selection; that override does not change
trusted installation metadata. This is a reduced-trust path because the selected image
receives the mounts and environment authorized for that run, so use it only with an image
you independently trust. Windows maintenance, update, and doctor paths remain trusted
digest only; they do not transfer persistent installation authority to an override.

The native launcher reports stable JSON failure categories when `--json` is selected:
`prerequisite`, `configuration`, `authentication`, `registry`, `mount`, `permission`,
`application`, and `launcher_protocol_mismatch`. Native launcher failures exit nonzero;
numeric application exits are listed in [Troubleshooting](troubleshooting.md).

## Maintenance commands

These commands are handled specially by a trusted installed launcher.

| Command | Syntax and behavior |
|---|---|
| `doctor` | `goalrouter doctor`; validates install, daemon, image, config, mounts, state, and normally account model inventory. POSIX optionally accepts `--skip-account`; Windows accepts `-SkipAccount`. |
| `update` | `goalrouter update [x.y.z|latest]`; runs the recorded installer and preserves compatible config and state. |
| `version` | `goalrouter version`; validates metadata, image presence, runtime protocol/version, and prints build identity. |
| `uninstall` | POSIX accepts `--yes` and `--purge`; Windows accepts `-Yes` and `-Purge`. |

## Application global options

Global application options are normally supplied by the launcher. `--config` selects the
container routing YAML, `--json` chooses JSON output, `--auth-mode` selects the explicit
authentication mode, `--state-path` changes the container state root, and `--codex-bin`
is an advanced explicit Codex binary path. `--help` prints parser help. Native users should
prefer the corresponding launcher options so host paths are resolved and mounted safely.

## Application commands

Application global options are `--config`, `--json`, `--auth-mode`, `--state-path`, and
advanced `--codex-bin`. Use application `--help` after a command to inspect its accepted
arguments.

| Command | Syntax and result |
|---|---|
| `config template` | Emits the shipped repository-neutral YAML template. |
| `config validate` | Validates schema, references, routing relationships, and prints config digest/task/model summary. |
| `version` | Prints application version, launcher protocol, image identity, and source revision metadata. |
| `models` | Authenticates, lists account models, and fails if a configured model is unavailable. |
| `route` | `route --project /project --task TASK --prompt TEXT [--affected-path PATH ...]`; previews one explicit route. |
| `plan` | `plan --project /project --objective TEXT [--run-id ID]`; inspects, decomposes, routes, validates, and persists without worker dispatch. |
| `run` | Uses `--project /project` and exactly one of `--task` or `--objective`; task mode also requires `--prompt`, while both modes accept `--run-id` and task mode accepts `--affected-path`. |
| `status` | `status RUN_ID`; reads the latest persisted run snapshot. |
| `approve` | `approve RUN_ID WORK_ITEM_ID --approved-by IDENTITY`; records one exact fingerprint-bound approval. |
| `resume` | `resume RUN_ID [--acknowledge-configuration-change]`; continues unfinished work without repeating terminal items. |
| `report` | `report RUN_ID`; renders and persists the current Markdown report. |

`status` is a non-mutating snapshot and does not acquire the run lease. Planning, `run`,
`approve`, `resume`, and `report` are mutating workflows and each holds that run's
nonblocking lease from before state creation/load through the final write.

Project writer contention fails immediately with exit `14`. Run contention fails
immediately with exit `15`. In either case, wait for the owning process or container to
exit before retrying. A project write lease creates no target lockfile; `status` can still
read the last atomic snapshot while another command owns the run.

Application `--json` belongs before the application command. With the native launcher,
the one launcher `--json` flag is forwarded correctly:

```text
goalrouter --project /path/to/project --access readonly --json route --project /project --task repository-search --prompt "Find tests"
```

Unknown options, missing values, unknown commands, incompatible run modes, invalid config,
unavailable models, authentication failure, corrupt state, and missing approval all fail
explicitly. There are no compatibility command aliases or silent fallback paths.
