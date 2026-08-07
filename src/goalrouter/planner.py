# SPDX-License-Identifier: MIT
# File: src/goalrouter/planner.py
# Purpose: Structured objective planning and semantic plan validation

"""Ask Codex for bounded work items and validate them before dispatch."""

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from jsonschema import Draft202012Validator

from goalrouter.config import RouterConfig
from goalrouter.domain import (
    AccessMode,
    Objective,
    RepositoryContext,
    WorkItem,
)
from goalrouter.errors import PlannerOutputError
from goalrouter.routing import TaskRouter
from goalrouter.sdk.protocol import CodexClientProtocol


class PlannerProtocol(Protocol):
    """Port for validated objective decomposition."""

    async def plan(
        self,
        objective: Objective,
        repository: RepositoryContext,
        config: RouterConfig,
    ) -> tuple[WorkItem, ...]: ...


class StructuredPlanner:
    """Use one Codex thread to produce schema-constrained work items."""

    def __init__(
        self,
        client: CodexClientProtocol,
        router: TaskRouter,
        *,
        schema_path: Path | None = None,
    ) -> None:
        self._client = client
        self._router = router
        self._schema_path = schema_path or Path(
            os.environ.get(
                "GOALROUTER_PLANNER_SCHEMA",
                "/etc/goalrouter/planner-output.schema.json",
            )
        )

    async def plan(
        self,
        objective: Objective,
        repository: RepositoryContext,
        config: RouterConfig,
    ) -> tuple[WorkItem, ...]:
        """Plan once, allowing one correction only for structural invalidity."""

        schema = _load_schema(self._schema_path)
        planner_item = _planner_work_item(objective, config)
        route = self._router.route(planner_item)
        prompt = _build_prompt(objective, repository, config, schema)
        result = await self._client.run_new_thread(
            project_path=objective.project_path,
            route=route,
            prompt=prompt,
            developer_instructions=(
                "Return only structured work items that satisfy the supplied JSON Schema. "
                "Do not assign concrete models; choose only configured task identifiers."
            ),
            output_schema=schema,
        )
        try:
            return _parse_and_validate(
                result.final_response,
                objective=objective,
                config=config,
                schema=schema,
            )
        except _StructuralPlannerOutputError as first_error:
            if result.thread_id is None:
                raise PlannerOutputError(
                    "Planner response was structurally invalid and had no resumable thread ID"
                ) from first_error
            corrected = await self._client.resume_thread(
                thread_id=result.thread_id,
                project_path=objective.project_path,
                route=route,
                prompt=(
                    "Correct the previous response to satisfy the supplied JSON schema exactly. "
                    f"Structural error: {first_error}"
                ),
                output_schema=schema,
            )
            return _parse_and_validate(
                corrected.final_response,
                objective=objective,
                config=config,
                schema=schema,
            )


def validate_plan_result(
    content: str,
    *,
    objective: Objective,
    config: RouterConfig,
    schema_path: Path,
) -> tuple[WorkItem, ...]:
    """Validate one planner response for callers that already own the turn."""

    return _parse_and_validate(
        content,
        objective=objective,
        config=config,
        schema=_load_schema(schema_path),
    )


class _StructuralPlannerOutputError(PlannerOutputError):
    """A response that may be corrected once without semantic guessing."""


def _load_schema(path: Path) -> Mapping[str, object]:
    try:
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PlannerOutputError(f"Cannot load planner schema {path}: {error}") from error
    return _mapping(loaded, f"planner schema {path}")


def _parse_and_validate(
    content: str | None,
    *,
    objective: Objective,
    config: RouterConfig,
    schema: Mapping[str, object],
) -> tuple[WorkItem, ...]:
    if content is None:
        raise _StructuralPlannerOutputError("Planner returned no final response")
    try:
        loaded: object = json.loads(content)
    except json.JSONDecodeError as error:
        raise _StructuralPlannerOutputError(
            f"Planner response is not valid JSON: {error}"
        ) from error
    raw = _mapping(loaded, "planner response")
    schema_error = next(iter(Draft202012Validator(schema).iter_errors(raw)), None)
    if schema_error is not None:
        location = ".".join(str(part) for part in schema_error.absolute_path) or "root"
        raise _StructuralPlannerOutputError(
            f"Planner response violates schema at {location}: {schema_error.message}"
        )
    return _parse_semantic_work_items(raw, objective=objective, config=config)


def _parse_semantic_work_items(
    raw: Mapping[str, object], *, objective: Objective, config: RouterConfig
) -> tuple[WorkItem, ...]:
    item_values = _sequence(_field(raw, "work-items"), "work-items")
    items: list[WorkItem] = []
    seen: set[str] = set()
    for index, item_value in enumerate(item_values):
        item_raw = _mapping(item_value, f"work-items[{index}]")
        item_id = _string(item_raw, "id")
        if item_id in seen:
            raise PlannerOutputError(f"Planner returned duplicate work-item ID {item_id!r}")
        seen.add(item_id)
        task = _string(item_raw, "task")
        if task not in config.tasks:
            raise PlannerOutputError(f"Planner returned unknown task {task!r}")
        access = AccessMode(_string(item_raw, "access"))
        verification = _string_tuple(item_raw, "verification")
        if access is AccessMode.WORKSPACE_WRITE and not verification:
            raise PlannerOutputError(
                f"Write-capable work item {item_id!r} has no verification"
            )
        affected_paths = tuple(
            _contained_relative_path(
                _plain_string(value, f"work-items[{index}].affected-paths"),
                project=objective.project_path,
            )
            for value in _sequence(_field(item_raw, "affected-paths"), "affected-paths")
        )
        items.append(
            WorkItem(
                id=item_id,
                title=_string(item_raw, "title"),
                instructions=_string(item_raw, "instructions"),
                task=task,
                phase=_string(item_raw, "phase"),
                dependencies=_string_tuple(item_raw, "dependencies"),
                access=access,
                affected_paths=affected_paths,
                expected_result=_string(item_raw, "expected-result"),
                verification=verification,
                confidence=_number(item_raw, "confidence"),
                risk_flags=frozenset(_string_tuple(item_raw, "risk-flags")),
            )
        )

    item_ids = {item.id for item in items}
    for item in items:
        for dependency in item.dependencies:
            if dependency not in item_ids:
                raise PlannerOutputError(
                    f"Work item {item.id!r} has missing dependency {dependency!r}"
                )
    _reject_cycles(items)
    return tuple(items)


def _reject_cycles(items: Sequence[WorkItem]) -> None:
    dependencies = {item.id: item.dependencies for item in items}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(item_id: str) -> None:
        if item_id in visiting:
            raise PlannerOutputError(f"Planner dependency cycle includes {item_id!r}")
        if item_id in visited:
            return
        visiting.add(item_id)
        for dependency in dependencies[item_id]:
            visit(dependency)
        visiting.remove(item_id)
        visited.add(item_id)

    for item in items:
        visit(item.id)


def _contained_relative_path(raw: str, *, project: Path) -> Path:
    project_root = project.resolve()
    candidate = Path(raw)
    resolved = (
        candidate.resolve()
        if candidate.is_absolute()
        else (project_root / candidate).resolve()
    )
    try:
        relative = resolved.relative_to(project_root)
    except ValueError as error:
        raise PlannerOutputError(f"Affected path escapes project: {raw}") from error
    return relative


def _planner_work_item(objective: Objective, config: RouterConfig) -> WorkItem:
    return WorkItem(
        id="objective-plan",
        title="Plan objective",
        instructions=objective.prompt,
        task=config.planner_task,
        phase="planning",
        dependencies=(),
        access=AccessMode.READ_ONLY,
        affected_paths=(),
        expected_result="A validated graph of bounded work items.",
        verification=(),
        confidence=1.0,
        risk_flags=frozenset(),
    )


def _build_prompt(
    objective: Objective,
    repository: RepositoryContext,
    config: RouterConfig,
    schema: Mapping[str, object],
) -> str:
    task_lines = "\n".join(
        f"- {name}: {policy.description}" for name, policy in config.tasks.items()
    )
    instruction_lines = "\n\n".join(
        f"## {item.path}\n{item.content}" for item in repository.instruction_files
    ) or "(none)"
    dirty_lines = "\n".join(path.as_posix() for path in repository.dirty_paths) or "(clean)"
    return (
        f"Objective:\n{objective.prompt}\n\n"
        f"Project path:\n{objective.project_path}\n\n"
        f"Repository instructions:\n{instruction_lines}\n\n"
        f"Dirty paths:\n{dirty_lines}\n\n"
        f"Configured task identifiers and descriptions:\n{task_lines}\n\n"
        "Return work items only. Choose task identifiers from the list above; do not assign "
        "concrete models. The response must satisfy this JSON Schema:\n"
        f"{json.dumps(schema, sort_keys=True)}"
    )


def _field(raw: Mapping[str, object], name: str) -> object:
    try:
        return raw[name]
    except KeyError as error:
        raise PlannerOutputError(f"Missing planner field {name}") from error


def _mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise _StructuralPlannerOutputError(f"Expected object for {context}")
    return {str(key): item for key, item in value.items()}


def _sequence(value: object, context: str) -> Sequence[object]:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise PlannerOutputError(f"Expected array for {context}")
    return value


def _plain_string(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise PlannerOutputError(f"Expected string for {context}")
    return value


def _string(raw: Mapping[str, object], name: str) -> str:
    return _plain_string(_field(raw, name), name)


def _string_tuple(raw: Mapping[str, object], name: str) -> tuple[str, ...]:
    return tuple(
        _plain_string(value, name) for value in _sequence(_field(raw, name), name)
    )


def _number(raw: Mapping[str, object], name: str) -> float:
    value = _field(raw, name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PlannerOutputError(f"Expected number for {name}")
    return float(value)
