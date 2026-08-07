# SPDX-License-Identifier: MIT
# File: tests/unit/test_planner.py
# Purpose: Verify structured planner prompts and semantic plan validation

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from goalrouter.config import RouterConfig, load_router_config
from goalrouter.domain import (
    InstructionFile,
    JsonValue,
    Objective,
    RepositoryContext,
    RouteDecision,
    WorkResult,
    WorkStatus,
)
from goalrouter.errors import PlannerOutputError
from goalrouter.planner import StructuredPlanner, validate_plan_result
from goalrouter.routing import TaskRouter

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/planner"
SCHEMA = ROOT / "config/planner-output.schema.json"


def _config() -> RouterConfig:
    return load_router_config(
        ROOT / "config/task-models.yaml",
        schema_path=ROOT / "config/task-models.schema.json",
    )


def _objective(project: Path) -> Objective:
    return Objective(
        id="run-1",
        prompt="Implement the requested behavior",
        project_path=project,
        explicit_task=None,
        config_path=ROOT / "config/task-models.yaml",
        created_at=datetime(2026, 8, 3, tzinfo=UTC),
    )


def _repository(project: Path) -> RepositoryContext:
    return RepositoryContext(
        project_path=project,
        is_git_worktree=True,
        branch="feature/example",
        dirty_paths=(Path("src/dirty.py"),),
        instruction_files=(
            InstructionFile(project / "AGENTS.md", "Use Docker for all execution."),
        ),
        language_counts=(("python", 2),),
        docker_files=(project / "Dockerfile",),
        command_errors=(),
    )


def _result(content: str) -> WorkResult:
    return WorkResult(
        work_item_id="objective-plan",
        thread_id="thread-plan",
        turn_id="turn-plan",
        status=WorkStatus.SUCCEEDED,
        final_response=content,
        sdk_items=(),
        input_tokens=10,
        cached_input_tokens=0,
        output_tokens=20,
        duration_seconds=1.0,
        changed_paths=(),
        verification=(),
        confidence=1.0,
        escalation_requested=False,
        error=None,
    )


class FakeCodexClient:
    def __init__(self, responses: list[WorkResult]) -> None:
        self.responses = responses
        self.new_calls: list[dict[str, object]] = []
        self.resume_calls: list[dict[str, object]] = []

    async def available_models(self) -> frozenset[str]:
        return frozenset(alias.model for alias in _config().model_aliases.values())

    async def run_new_thread(
        self,
        *,
        project_path: Path,
        route: RouteDecision,
        prompt: str,
        developer_instructions: str,
        output_schema: Mapping[str, object] | None = None,
    ) -> WorkResult:
        self.new_calls.append(
            {
                "project_path": project_path,
                "route": route,
                "prompt": prompt,
                "developer_instructions": developer_instructions,
                "output_schema": output_schema,
            }
        )
        return self.responses.pop(0)

    async def resume_thread(
        self,
        *,
        thread_id: str,
        project_path: Path,
        route: RouteDecision,
        prompt: str,
        output_schema: Mapping[str, object] | None = None,
    ) -> WorkResult:
        self.resume_calls.append(
            {
                "thread_id": thread_id,
                "project_path": project_path,
                "route": route,
                "prompt": prompt,
                "output_schema": output_schema,
            }
        )
        return self.responses.pop(0)


def _validate(content: str, project: Path) -> tuple[str, ...]:
    items = validate_plan_result(
        content,
        objective=_objective(project),
        config=_config(),
        schema_path=SCHEMA,
    )
    return tuple(item.id for item in items)


def test_valid_plan_parses_to_dependency_ordered_work_items(tmp_path: Path) -> None:
    content = (FIXTURES / "valid-plan.json").read_text(encoding="utf-8")

    assert _validate(content, tmp_path) == ("inspect", "implement")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda raw: raw["work-items"][0].update({"task": "absent-task"}), "absent-task"),
        (lambda raw: raw["work-items"][1].update({"id": "inspect"}), "duplicate"),
        (lambda raw: raw["work-items"][1].update({"dependencies": ["absent"]}), "absent"),
        (lambda raw: raw["work-items"][1].update({"verification": []}), "verification"),
        (lambda raw: raw["work-items"][0].update({"confidence": 1.1}), "confidence"),
    ],
)
def test_rejects_invalid_structural_and_semantic_plan_values(
    tmp_path: Path,
    mutation: object,
    message: str,
) -> None:
    raw: dict[str, JsonValue] = json.loads(
        (FIXTURES / "valid-plan.json").read_text(encoding="utf-8")
    )
    assert callable(mutation)
    mutation(raw)

    with pytest.raises(PlannerOutputError, match=message):
        _validate(json.dumps(raw), tmp_path)


@pytest.mark.parametrize(
    "fixture",
    ["cycle.json", "path-escape.json"],
)
def test_rejects_cycles_and_paths_outside_project(tmp_path: Path, fixture: str) -> None:
    with pytest.raises(PlannerOutputError):
        _validate((FIXTURES / fixture).read_text(encoding="utf-8"), tmp_path)


def test_rejects_invalid_json(tmp_path: Path) -> None:
    with pytest.raises(PlannerOutputError, match="JSON"):
        _validate("not-json", tmp_path)


@pytest.mark.asyncio
async def test_prompt_contains_evidence_tasks_and_schema_but_not_model_assignment(
    tmp_path: Path,
) -> None:
    valid = (FIXTURES / "valid-plan.json").read_text(encoding="utf-8")
    client = FakeCodexClient([_result(valid)])
    planner = StructuredPlanner(client, TaskRouter(_config()), schema_path=SCHEMA)

    items = await planner.plan(_objective(tmp_path), _repository(tmp_path), _config())

    assert tuple(item.id for item in items) == ("inspect", "implement")
    call = client.new_calls[0]
    prompt = str(call["prompt"])
    assert "Implement the requested behavior" in prompt
    assert "Use Docker for all execution." in prompt
    assert "src/dirty.py" in prompt
    assert "repository-search" in prompt
    assert "Locate files, symbols, tests, or exact text." in prompt
    assert "work-items" in prompt
    assert all(alias.model not in prompt for alias in _config().model_aliases.values())
    assert call["output_schema"] is not None


@pytest.mark.asyncio
async def test_schema_failure_gets_exactly_one_correction_turn(tmp_path: Path) -> None:
    valid = (FIXTURES / "valid-plan.json").read_text(encoding="utf-8")
    client = FakeCodexClient([_result("not-json"), _result(valid)])
    planner = StructuredPlanner(client, TaskRouter(_config()), schema_path=SCHEMA)

    items = await planner.plan(_objective(tmp_path), _repository(tmp_path), _config())

    assert len(items) == 2
    assert len(client.new_calls) == 1
    assert len(client.resume_calls) == 1
    assert "schema" in str(client.resume_calls[0]["prompt"]).casefold()


@pytest.mark.asyncio
async def test_semantic_failure_is_not_corrected(tmp_path: Path) -> None:
    raw = json.loads((FIXTURES / "valid-plan.json").read_text(encoding="utf-8"))
    raw["work-items"][0]["task"] = "absent-task"
    client = FakeCodexClient([_result(json.dumps(raw))])
    planner = StructuredPlanner(client, TaskRouter(_config()), schema_path=SCHEMA)

    with pytest.raises(PlannerOutputError, match="absent-task"):
        await planner.plan(_objective(tmp_path), _repository(tmp_path), _config())

    assert client.resume_calls == []
