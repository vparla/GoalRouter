# SPDX-License-Identifier: MIT
# File: tests/unit/test_domain.py
# Purpose: Verify GoalRouter domain values and explicit state serialization

import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path

import pytest

from goalrouter.domain import (
    AccessMode,
    ApprovalMode,
    ApprovalRecord,
    AuthMode,
    HardRiskRule,
    InstructionFile,
    MatchRule,
    ModelAlias,
    Objective,
    RepositoryContext,
    RouteDecision,
    RouteSource,
    RunState,
    RunStatus,
    SandboxMode,
    TaskPolicy,
    WorkItem,
    WorkResult,
    WorkStatus,
)


def test_enum_values_are_stable_kebab_case() -> None:
    assert AuthMode.EXISTING_SESSION.value == "existing-session"
    assert AuthMode.API_KEY.value == "api-key"
    assert SandboxMode.READ_ONLY.value == "read-only"
    assert SandboxMode.WORKSPACE_WRITE.value == "workspace-write"
    assert ApprovalMode.AUTOMATIC.value == "automatic"
    assert ApprovalMode.REQUIRED.value == "required"
    assert AccessMode.READ_ONLY.value == "read-only"
    assert AccessMode.WORKSPACE_WRITE.value == "workspace-write"
    assert WorkStatus.AWAITING_APPROVAL.value == "awaiting-approval"
    assert RunStatus.COMPLETED.value == "completed"
    assert RouteSource.EXPLICIT.value == "explicit"


def test_stable_domain_values_are_frozen_and_slotted() -> None:
    objective = Objective(
        id="run-1",
        prompt="Inspect the project",
        project_path=Path("/projects/example"),
        explicit_task=None,
        config_path=Path("/etc/goalrouter/task-models.yaml"),
        created_at=datetime(2026, 8, 3, tzinfo=UTC),
    )

    with pytest.raises(FrozenInstanceError):
        objective.prompt = "changed"  # type: ignore[misc]

    assert not hasattr(objective, "__dict__")


def test_run_state_round_trips_through_explicit_json_values() -> None:
    timestamp = datetime(2026, 8, 3, 18, 0, tzinfo=UTC)
    project = Path("/projects/example")
    objective = Objective(
        id="run-1",
        prompt="Implement and verify the change",
        project_path=project,
        explicit_task=None,
        config_path=Path("/etc/goalrouter/task-models.yaml"),
        created_at=timestamp,
    )
    repository = RepositoryContext(
        project_path=project,
        is_git_worktree=True,
        branch="feature/example",
        dirty_paths=(Path("src/existing.py"),),
        instruction_files=(
            InstructionFile(path=project / "AGENTS.md", content="Use Docker."),
        ),
        language_counts=(("python", 3),),
        docker_files=(project / "Dockerfile",),
        command_errors=(),
    )
    item = WorkItem(
        id="work-1",
        title="Implement change",
        instructions="Change the implementation.",
        task="python-coding",
        phase="implementation",
        dependencies=(),
        access=AccessMode.WORKSPACE_WRITE,
        affected_paths=(Path("src/example.py"),),
        expected_result="The behavior is implemented.",
        verification=("pytest tests/unit/test_example.py",),
        confidence=0.9,
        risk_flags=frozenset({"public-contract"}),
    )
    route = RouteDecision(
        task="python-coding",
        model_alias="frontier",
        model="example-frontier-model",
        reasoning_effort="high",
        sandbox=SandboxMode.WORKSPACE_WRITE,
        approval=ApprovalMode.REQUIRED,
        timeout_seconds=3600,
        max_attempts=1,
        destructive=False,
        external_write=False,
        escalation_task=None,
        source=RouteSource.PLANNER,
        reason="planner selected python-coding",
    )
    result = WorkResult(
        work_item_id="work-1",
        thread_id="thread-1",
        turn_id="turn-1",
        status=WorkStatus.SUCCEEDED,
        final_response="Implemented and verified.",
        sdk_items=({"type": "message", "text": "safe"},),
        input_tokens=100,
        cached_input_tokens=20,
        output_tokens=40,
        duration_seconds=12.5,
        changed_paths=(Path("src/example.py"),),
        verification=("1 passed",),
        confidence=0.95,
        escalation_requested=False,
        error=None,
    )
    approval = ApprovalRecord(
        run_id="run-1",
        work_item_id="work-1",
        approved_by="developer@example.test",
        approved_at=timestamp,
        configuration_digest="digest",
        fingerprint="fingerprint",
    )
    state = RunState(
        schema_version=1,
        configuration_digest="digest",
        objective=objective,
        repository=repository,
        work_items={item.id: item},
        routes={item.id: route},
        results={item.id: result},
        approvals={item.id: approval},
        status=RunStatus.COMPLETED,
    )

    serialized = state.to_dict()

    assert json.loads(json.dumps(serialized)) == serialized
    assert RunState.from_dict(serialized) == state


def test_policy_values_are_frozen_and_slotted() -> None:
    alias = ModelAlias(name="economy", model="example-model", reasoning_effort="low", rank=10)
    policy = TaskPolicy(
        task="repository-search",
        description="Search the repository.",
        model_alias="economy",
        sandbox=SandboxMode.READ_ONLY,
        approval=ApprovalMode.AUTOMATIC,
        timeout_seconds=600,
        max_attempts=1,
        destructive=False,
        external_write=False,
        escalate_to=None,
    )
    match = MatchRule(task="repository-search", phrases=("find file",), file_globs=())
    hard_risk = HardRiskRule(
        flag="security",
        minimum_model_alias="frontier",
        approval=ApprovalMode.REQUIRED,
    )

    assert all(not hasattr(value, "__dict__") for value in (alias, policy, match, hard_risk))
