# SPDX-License-Identifier: MIT
# File: tests/unit/test_approvals.py
# Purpose: Verify explicit approvals remain bound to immutable dispatch inputs

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from goalrouter.approvals import ApprovalService
from goalrouter.domain import (
    AccessMode,
    ApprovalMode,
    Objective,
    RepositoryContext,
    RouteDecision,
    RouteSource,
    RunState,
    RunStatus,
    SandboxMode,
    WorkItem,
)
from goalrouter.errors import ApprovalRequiredError


def _item() -> WorkItem:
    return WorkItem(
        id="write",
        title="Write",
        instructions="Make the bounded change.",
        task="implementation",
        phase="implementation",
        dependencies=(),
        access=AccessMode.WORKSPACE_WRITE,
        affected_paths=(Path("src/example.py"),),
        expected_result="A tested change.",
        verification=("pytest",),
        confidence=0.9,
        risk_flags=frozenset({"external-write"}),
    )


def _route(approval: ApprovalMode = ApprovalMode.REQUIRED) -> RouteDecision:
    return RouteDecision(
        task="implementation",
        model_alias="capable",
        model="example-model",
        reasoning_effort="high",
        sandbox=SandboxMode.WORKSPACE_WRITE,
        approval=approval,
        timeout_seconds=300,
        max_attempts=1,
        destructive=False,
        external_write=True,
        escalation_task=None,
        source=RouteSource.EXPLICIT,
        reason="explicit task",
    )


def _state() -> RunState:
    item = _item()
    return RunState(
        schema_version=1,
        configuration_digest="config-digest",
        objective=Objective(
            id="run-1",
            prompt="Make a change",
            project_path=Path("/project"),
            explicit_task=None,
            config_path=Path("/config.yaml"),
            created_at=datetime(2026, 8, 3, tzinfo=UTC),
        ),
        repository=RepositoryContext(
            project_path=Path("/project"),
            is_git_worktree=True,
            branch="main",
            dirty_paths=(),
            instruction_files=(),
            language_counts=(),
            docker_files=(),
            command_errors=(),
        ),
        work_items={item.id: item},
        routes={item.id: _route()},
        results={},
        approvals={},
        status=RunStatus.PLANNED,
    )


def test_required_approval_is_rejected_until_bound_record_exists() -> None:
    state = _state()
    service = ApprovalService()

    with pytest.raises(ApprovalRequiredError, match="write"):
        service.require_dispatchable(state, "write")

    approved_at = datetime(2026, 8, 3, 12, tzinfo=UTC)
    record = service.approve(
        state,
        work_item_id="write",
        approved_by="vinny@example.com",
        approved_at=approved_at,
    )

    assert record.run_id == "run-1"
    assert record.configuration_digest == "config-digest"
    assert record.approved_by == "vinny@example.com"
    assert record.approved_at == approved_at
    assert len(record.fingerprint) == 64
    assert state.approvals["write"] is record
    service.require_dispatchable(state, "write")


def test_approval_is_invalid_after_configuration_or_route_change() -> None:
    service = ApprovalService()
    state = _state()
    service.approve(
        state,
        work_item_id="write",
        approved_by="operator",
        approved_at=datetime(2026, 8, 3, tzinfo=UTC),
    )

    state.configuration_digest = "different"
    with pytest.raises(ApprovalRequiredError, match="configuration"):
        service.require_dispatchable(state, "write")

    state.configuration_digest = "config-digest"
    state.routes["write"] = replace(state.routes["write"], timeout_seconds=301)
    with pytest.raises(ApprovalRequiredError, match="fingerprint"):
        service.require_dispatchable(state, "write")


def test_automatic_route_does_not_require_an_approval_record() -> None:
    state = _state()
    state.routes["write"] = _route(ApprovalMode.AUTOMATIC)

    ApprovalService().require_dispatchable(state, "write")

    assert state.approvals == {}
