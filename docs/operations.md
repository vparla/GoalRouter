<!-- SPDX-License-Identifier: MIT -->
<!-- File: docs/operations.md -->
<!-- Purpose: Installed GoalRouter operating procedures -->

# Operations

Use the installed native launcher for normal work. Begin with `--access readonly` and
select a broader host mount only when the exact work requires it. Launcher options precede
the application command; application options follow it.

## Preflight

```text
goalrouter version
goalrouter doctor
goalrouter config validate
goalrouter models
```

`version` checks trusted install metadata and the runtime protocol. `doctor` checks Docker,
the pinned image, configuration, mounts, writable state, and model inventory. `config
validate` is local and non-billable. `models` contacts the selected Codex account but does
not start an agent turn.

## Preview one route

The first project path is a host launcher option. `/project` is the application path inside
the runtime.

```text
goalrouter --project /path/to/repository --access readonly --json route --project /project --task repository-search --prompt "Locate the service boundary" --affected-path src/service.py
```

A preview validates the whole configured model inventory, applies task policy and hard-risk
floors, and returns the route without inspecting the project, creating run state, or
dispatching work.

## Plan an objective

```text
goalrouter --project /path/to/repository --access readonly plan --project /project --objective "Implement the bounded change and verify it" --run-id change-plan
goalrouter --access readonly status change-plan
goalrouter --access readonly report change-plan
```

Planning inspects repository metadata and applicable instruction files before it validates
model inventory. It does not execute target code. After those checks, it asks the
configured planner for schema-constrained work items, validates dependencies, routes every
item, and persists a `planned` run. Worker items do not start until `run` or `resume`
continues the run.

Instruction preflight completes before GoalRouter starts Git. Dirty paths are composed from
strict NUL-delimited index, HEAD-tree, and untracked-name metadata plus GoalRouter-owned raw
blob hashing through already-open no-follow descriptors. Git conversion filters never receive
tracked worktree content. Hash helpers are killed and reaped on timeout or cancellation, no
optional index lock is taken, and nested submodule worktree dirtiness is ignored; staged gitlink
changes remain visible. When end-of-line normalization or a clean/process filter changes the
index representation, raw comparison can conservatively report the path as dirty. Inspect a
submodule as a separate explicit project when its nested worktree state matters.

## Run work

One explicit read-only task:

```text
goalrouter --project /path/to/repository --access readonly run --project /project --task repository-search --prompt "Find the implementation and tests" --run-id search-run
```

One explicit write-capable task, after reviewing the route and intended paths:

```text
goalrouter --project /path/to/repository --access write run --project /project --task documentation --prompt "Update the operator guide and verify its examples" --affected-path docs/operations.md --run-id docs-run
```

A compound objective:

```text
goalrouter --project /path/to/repository --access write run --project /project --objective "Implement the bounded change and run its verification" --run-id objective-run
```

Compound execution plans first, runs dependency-ready work, serializes writers, batches
independent readers up to `maximum-read-concurrency`, checkpoints each terminal item, and
adds a separate read-only completion review after base work succeeds.

The launcher mount is a hard ceiling. `readonly` cannot perform a workspace-write route.
`write` does not expose the Docker socket. Use `docker` only for an explicit task that must
invoke the daemon:

```text
goalrouter --project /path/to/repository --access docker run --project /project --task docker-invoke --prompt "Run the existing bounded container verification" --run-id docker-run
```

Docker socket access carries broad host-container authority. Review the command, target,
and cleanup scope before selecting it.

## Concurrent commands and writers

Planning, explicit task/objective execution, approval, resume, and report hold one
nonblocking lease for the selected run. If another process or container already owns that
run, the command fails immediately with exit `15` before it loads mutable state or starts a
Codex call. `status` does not take this lease because it only loads the current atomic
snapshot.

Write-capable items also take a nonblocking kernel lease on the physical project directory.
A different run, state root, process, or container that reaches the same project writer
while it is owned fails immediately with exit `14`, without dispatching that writer or
creating a failed work result for it. Reader work completed before the writer becomes ready
remains recorded. Read-only batches do not take this project lease and retain bounded
concurrency. The project directory lease creates no lockfile or sentinel in the target
project.

When an approved writer reaches project contention from `awaiting-approval` or transient
`running`, its checkpoint is normalized to `planned`; `failed` and `blocked` states are not
rewritten. The existing approval remains valid, so wait for the owner to exit and resume
the same run. Do not delete the persistent run lockfile or create a target lock artifact;
kernel ownership, rather than file presence, identifies the active owner.

## Status and reports

```text
goalrouter --access readonly status RUN_ID
goalrouter --access readonly --json status RUN_ID
goalrouter --access readonly report RUN_ID
```

Each run directory contains atomic `state.json`, append-only `events.jsonl`, and generated
`report.md`. Reports include repository evidence, route policy, approval state, results,
changed paths, verification, safe SDK summaries, timing, and token usage. Known credential
shapes are redacted before persistence.

`status` is a non-mutating snapshot that remains available while another process owns the
run lease. The snapshot may show the last completed checkpoint rather than in-memory work
that has not yet been persisted.

## Approval

An approval-required item remains pending until its exact current work item, route, and
configuration digest are approved:

```text
goalrouter --access readonly status RUN_ID
goalrouter --access readonly approve RUN_ID WORK_ITEM_ID --approved-by operator@example.com
goalrouter --project /path/to/repository --access write resume RUN_ID
```

Approval records identity and time but does not resume automatically. It does not broaden
the launcher mount, add the Docker socket, authorize another item, or survive a changed
fingerprint.

## Resume and configuration drift

```text
goalrouter --project /path/to/repository --access write resume RUN_ID
```

Resume skips terminal work. If an interrupted item has a stored Codex thread ID, GoalRouter
resumes that thread; otherwise it starts a new thread for pending work. A different routing
configuration is refused with exit 13. After reviewing the new policy, explicitly adopt it:

```text
goalrouter --project /path/to/repository --access write resume RUN_ID --acknowledge-configuration-change
```

Acknowledgement reroutes unfinished items and invalidates all prior approvals. It does not
alter results already terminal.

## Operational recovery

- If authentication fails, follow [Authentication](authentication.md); never change modes
  implicitly.
- If state is corrupt, preserve it for diagnosis and start a distinct run ID rather than
  hand-editing partial JSON.
- If policy changed, compare the old digest and intended new routes before acknowledging.
- If a work item failed, inspect its report and dependency state before creating new work.
- If a command reports run or project contention, wait for the owning process or container
  to exit, then retry the same command. Do not remove lease files or add a project marker.
- If installation metadata or image presence fails, use the trusted update/repair process
  in [Upgrading](upgrading.md).
- If uninstall was interrupted, retry with the exact original preserve/purge mode as
  described in [Uninstalling](uninstalling.md).
