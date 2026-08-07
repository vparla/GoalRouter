<!-- SPDX-License-Identifier: MIT -->
<!-- File: docs/troubleshooting.md -->
<!-- Purpose: GoalRouter launcher categories, stable exits, and recovery actions -->

# Troubleshooting

Start with:

```text
goalrouter version
goalrouter doctor
goalrouter --json doctor
```

Without `--json`, native launcher failures print one concise stderr message. With JSON,
they return a sanitized object with `status`, `code`, and `message`.

## Native launcher categories

| Category | Meaning | Action |
|---|---|---|
| `prerequisite` | Docker, WSL, host directory, or required runtime prerequisite is unavailable. | Confirm Docker/WSL and rerun doctor. |
| `configuration` | Config path, YAML, schema, or trusted config metadata failed. | Run config validation and inspect the selected file. |
| `authentication` | Selected Codex session or key mode failed. | Follow the exact selected mode in Authentication. |
| `registry` | Named image, digest, pull, or manifest failed. | Confirm network/registry access and trusted install metadata. |
| `mount` | A project, state, config, or Codex path cannot be safely resolved or mounted. | Use an existing exact local path; avoid links and unsupported providers. |
| `permission` | An owned path, state write, read-only boundary, owner, or ACL check failed. | Restore intended ownership/permissions; do not broaden authority blindly. |
| `application` | The runtime returned another failure. | Run the application command directly through the launcher with JSON and inspect state/report. |
| `launcher_protocol_mismatch` | Native launcher protocol 1 cannot run the image protocol. | Update the installed launcher and image together. |

## Stable application exit codes

| Exit | Failure | Action |
|---:|---|---|
| 1 | General GoalRouter failure | Read the concise error and persisted events. |
| 2 | Invalid configuration or CLI mode | Validate YAML, references, and required arguments. |
| 3 | Unknown task | Add or correct the configured task identifier. |
| 4 | Configured model unavailable | Run `goalrouter models` and choose models visible to the selected account. |
| 5 | Repository inspection failure | Confirm `/project` is the mounted readable target and source-control metadata is valid. |
| 6 | Invalid planner output | Review the structural/semantic failure; do not invent replacement items. |
| 7 | Approval required or stale | Review and approve the exact current fingerprint. |
| 8 | Dependency blocked | Resolve the failed prerequisite before retrying dependent work. |
| 9 | Codex SDK failure | Inspect SDK lifecycle, model, binary override, and final error. |
| 10 | Authentication failure | Repair the explicitly selected authentication mode. |
| 11 | Turn timeout | Review `timeout-seconds`; do not silently change policy. |
| 12 | Corrupt or unsupported state | Preserve the run for diagnosis or start a new ID; do not hand-edit partial JSON. |
| 13 | Resume configuration changed | Review new policy, then acknowledge rerouting and approval invalidation explicitly. |
| 14 | Project writer busy | Another process or container owns the physical project's write lease; wait for it to exit. |
| 15 | Run busy | Another process or container owns this run's mutation lease; wait before retrying. |

## Common cases

### Existing session is unavailable

Run `goalrouter doctor`. Confirm the installed Codex-home location is the one used by your
local Codex sign-in. Use the official `codex login` browser flow if needed, then retry.
GoalRouter will not ask for a key or switch modes automatically.

### State is not writable

The runtime image declares a non-root UID/GID, but the POSIX launcher maps the invoking
UID/GID into the container; root invocation runs the container as root. Invoke GoalRouter
as a non-root user and restore safe user ownership and permissions on the recorded writable
state directory. Do not mount the whole home or use root invocation as a permission fix.

### Project changes fail under readonly access

This is expected. Preview the route, verify its sandbox is `workspace-write`, review
approval requirements, then deliberately rerun with `--access write`. Use `docker` access
only if the exact task needs the daemon.

### Resume does not repeat an item

Terminal items are deliberately skipped. Interrupted running items with a stored Codex
thread ID resume that thread. Changed configuration requires explicit acknowledgement and
clears pending approvals.

### A run or project is busy

Exit `15` means another mutating command owns the same run. Exit `14` means a writer from
this or another run owns the same physical project. Both failures are immediate and occur
before the contending SDK dispatch. `status RUN_ID` remains available as a non-mutating
snapshot, although it can show only the last persisted checkpoint.

Retry only after the process or container holding the lease exits; kernel ownership is
then released even after a crash. Do not delete `.locks/runs/RUN_ID.lock`: the stable file
must remain so every process addresses one inode. Project ownership uses the directory
itself and creates no project lockfile to remove. If no owner is expected, confirm that the
original container or process has actually exited before diagnosing a filesystem/daemon
failure.

### Repository instruction is unsafe

Exit `5` can identify `AGENTS.md` or `SKILLS.md` as an unsafe symbolic link, ancestor,
directory, FIFO, socket, device, or unreadable regular file. Plan and run-creation
workflows reject it before those workflows make a Codex SDK or model call and do not echo
linked content or the link target. Replace the entry with a normal UTF-8 regular file
inside the repository, or remove the optional file; do not broaden the authentication
mount or follow the link manually as a workaround.

### Repository Git inspection is refused

Exit `5` during Git inspection can indicate an unsafe `.git` entry, malformed discovered
paths, output limit, timeout, or source-control ownership refusal. Repair repository
metadata with trusted Git tooling; do not add `safe.directory=*` or delete another
worktree's administrative files. A linked worktree must retain its normal regular `.git`
gitfile and reachable administrative directory. If only nested submodule worktree changes
are missing from dirty-path evidence, inspect that submodule explicitly; parent inspection
intentionally does not recurse into it.

### Update or uninstall reports corrupt control

Stop and preserve the installation directories. Trusted lifecycle commands fail closed on
missing checksums, path drift, unsafe ownership, links, and incomplete recovery. Use the
documented force repair only after verifying exact destinations; never fabricate metadata.
