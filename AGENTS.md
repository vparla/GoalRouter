# SPDX-License-Identifier: MIT
<!-- File: AGENTS.md -->
<!-- Purpose: Durable engineering rules for GoalRouter -->

# GoalRouter Engineering Rules

## Runtime and execution

- Python 3.14 is the only supported runtime.
- Run dependency installation, tests, linting, type checks, package builds, the CLI, and live SDK checks in the declared Docker lifecycle.
- On Windows, WSL routes Docker only. Do not run Python or package tooling directly in WSL or on the host.
- Treat every warning as a failure.

## Architecture

- Search for and extend shared capability before adding another implementation.
- Use typing.Protocol for cross-module ports and constructor injection at composition roots.
- Use frozen, slotted dataclasses for stable domain values.
- Keep OpenAI Codex SDK imports within src/goalrouter/sdk.
- Keep task names, model identifiers, reasoning effort, sandbox, approval, timeout, matching, and escalation policy in YAML.
- Do not add repository names or repository paths to the routing YAML.
- Fail explicitly on invalid configuration, unknown tasks, unavailable models, authentication failure, corrupt state, and approval requirements.
- Do not add compatibility aliases or silent fallback paths.

## Async behavior

- All I/O is async at public service boundaries.
- Use asyncio.TaskGroup for owned sibling work.
- Use asyncio.timeout for timeout scopes.
- Use asyncio.to_thread only to isolate unavoidable blocking filesystem operations.
- Do not use polling loops, asyncio.gather, asyncio.get_event_loop, or unowned background tasks.
- Permit only one write-capable work item per target project at a time.

## Testing and documentation

- Develop production behavior test-first and observe the expected failure before implementation.
- Use structural fakes that satisfy Protocol boundaries.
- Preserve dirty target worktrees.
- Do not commit, push, merge, publish, mutate secrets, or clean target state without explicit authorization.
- Keep planning documents under ignored planning/ and never reference them from tracked documentation.
- Keep README.md and docs/ self-contained and Docker-only.
