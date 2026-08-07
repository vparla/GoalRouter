<!-- SPDX-License-Identifier: MIT -->
<!-- File: docs/configuration.md -->
<!-- Purpose: Complete GoalRouter routing and planner schema reference -->

# Configuration

GoalRouter validates routing YAML against JSON Schema Draft 2020-12 before composing any
application service. Routing policy is defined by `task-models.schema.json`; structured
planner responses are defined by `planner-output.schema.json`. The installed config is
user-owned and repository-neutral: task
names, model IDs, reasoning effort, sandbox, approval, timeout, attempts, matching, and
escalation belong here; repository names and paths do not.

Print a fresh template or validate the active file with the installed launcher:

```text
goalrouter config template
goalrouter config validate
goalrouter --json config validate
```

## Complete schema field reference

The table is the union of the routing schema and structured planner-output schema. A field
is accepted only in the object described; both schemas reject unexpected properties.

| Field | Object | Meaning |
|---|---|---|
| `schema-version` | routing root | Configuration format; version 1 is supported. |
| `default-task` | routing root | Fallback configured task. |
| `planner-task` | routing root | Task used for structured decomposition. |
| `completion-task` | routing root | Task used for independent read-only completion review. |
| `maximum-read-concurrency` | routing root | Positive bound for sibling read-only work. |
| `repository-inspection-timeout-seconds` | routing root | Positive timeout for each repository filesystem or Git inspection phase. |
| `model-aliases` | routing root | Nonempty mapping of stable policy aliases to concrete models. |
| `tasks` | routing root | Nonempty mapping of arbitrary kebab-case task IDs to policies. |
| `matching` | routing root | Optional ordered match rules; defaults to empty. |
| `hard-risk-rules` | routing root | Optional global capability and approval floors. |
| `model` | model alias | Exact concrete account model identifier. |
| `reasoning-effort` | model alias | `low`, `medium`, `high`, `xhigh`, `max`, or `ultra`. |
| `rank` | model alias | Unique positive capability order used by minimum floors. |
| `description` | task policy | Nonempty planner-visible task purpose. |
| `model-alias` | task policy | Existing alias selected by the task. |
| `sandbox` | task policy | Exactly `read-only` or `workspace-write`. |
| `approval` | task/risk policy | Exactly `automatic` or `required`. |
| `timeout-seconds` | task policy | Positive timeout for each SDK turn. |
| `max-attempts` | task policy | Positive attempt policy recorded in the route. |
| `destructive` | task policy | Optional Boolean that activates destructive risk. |
| `external-write` | task policy | Optional Boolean that activates external-write risk. |
| `escalate-to` | task policy | Optional existing task used for explicit escalation policy. |
| `task` | match/planner item | Configured task identifier selected by the rule or planner. |
| `phrases` | match rule | Unique nonempty strings matched in prompt text. |
| `file-globs` | match rule | Unique nonempty path patterns matched in affected paths. |
| `flag` | hard-risk rule | Risk identifier to which the floor applies. |
| `minimum-model-alias` | hard-risk rule | Optional minimum capability alias. |
| `work-items` | planner root | Nonempty list of planned work-item objects. |
| `id` | planner item | Unique kebab-case item identifier. |
| `title` | planner item | Nonempty concise title. |
| `instructions` | planner item | Nonempty bounded execution instructions. |
| `phase` | planner item | Kebab-case phase identifier. |
| `dependencies` | planner item | Unique item IDs that must succeed first. |
| `access` | planner item | Exactly `read-only` or `workspace-write`. |
| `affected-paths` | planner item | Unique nonempty repository-relative path strings. |
| `expected-result` | planner item | Nonempty completion outcome. |
| `verification` | planner item | Unique nonempty evidence requirements. |
| `confidence` | planner item | Number from 0 through 1. |
| `risk-flags` | planner item | Unique kebab-case risks evaluated by hard floors. |

## Cross-reference and semantic rules

`default-task`, `planner-task`, `completion-task`, match targets, and `escalate-to` must
reference defined tasks. Task aliases and minimum aliases must exist. Alias ranks are
unique. Escalation cycles are invalid. Matching rules require phrases, file globs, or
both. Planned IDs and dependencies must be valid and acyclic; an item cannot depend on
itself or name an unknown dependency.

Routing precedence is explicit CLI task, validated planner task, first matching rule, then
`default-task`. A hard-risk rule may raise the model floor or require approval; downstream
policy cannot weaken that floor. `destructive` and `external-write` task declarations add
their corresponding risk flags.

## Extend policy safely

Start from `goalrouter config template`, add an arbitrary kebab-case task, reuse or add a
model alias, and optionally add an ordered match rule. Validate before use. Concrete model
availability is account-specific, so run `goalrouter models` after validation. GoalRouter
does not invent a substitute model, task, or legacy alias when validation fails.

Keep credentials, target paths, repository identities, and operator-specific commands out
of routing YAML. Authentication is described in [Authentication](authentication.md).
