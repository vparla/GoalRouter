# SPDX-License-Identifier: MIT
# File: tests/unit/test_reporting.py
# Purpose: Verify deterministic, complete, and redacted run reports

from datetime import UTC, datetime
from pathlib import Path

from goalrouter.domain import (
    AccessMode,
    ApprovalMode,
    InstructionFile,
    Objective,
    RepositoryContext,
    RouteDecision,
    RouteSource,
    RunState,
    RunStatus,
    SandboxMode,
    WorkItem,
    WorkResult,
    WorkStatus,
)
from goalrouter.reporting import ReportRenderer


def test_report_contains_routes_results_evidence_and_totals() -> None:
    item = WorkItem(
        id="inspect",
        title="Inspect service",
        instructions="Inspect the service boundary.",
        task="repository-search",
        phase="discovery",
        dependencies=(),
        access=AccessMode.READ_ONLY,
        affected_paths=(Path("src/service.py"),),
        expected_result="Evidence",
        verification=("review output",),
        confidence=0.8,
        risk_flags=frozenset(),
    )
    route = RouteDecision(
        task="repository-search",
        model_alias="economy",
        model="example-model",
        reasoning_effort="low",
        sandbox=SandboxMode.READ_ONLY,
        approval=ApprovalMode.AUTOMATIC,
        timeout_seconds=30,
        max_attempts=1,
        destructive=False,
        external_write=False,
        escalation_task=None,
        source=RouteSource.MATCH,
        reason="matching rule 0",
    )
    result = WorkResult(
        work_item_id="inspect",
        thread_id="thread-1",
        turn_id="turn-1",
        status=WorkStatus.SUCCEEDED,
        final_response="Found the boundary.",
        sdk_items=({"type": "message", "api_key": "sk-secret-value"},),
        input_tokens=10,
        cached_input_tokens=2,
        output_tokens=5,
        duration_seconds=1.25,
        changed_paths=(Path("src/service.py"),),
        verification=("reviewed",),
        confidence=0.95,
        escalation_requested=False,
        error=None,
    )
    state = RunState(
        schema_version=1,
        configuration_digest="digest",
        objective=Objective(
            id="run-1",
            prompt="Understand the service",
            project_path=Path("/project"),
            explicit_task=None,
            config_path=Path("/config.yaml"),
            created_at=datetime(2026, 8, 3, tzinfo=UTC),
        ),
        repository=RepositoryContext(
            project_path=Path("/project"),
            is_git_worktree=True,
            branch="main",
            dirty_paths=(Path("dirty.txt"),),
            instruction_files=(
                InstructionFile(Path("/project/AGENTS.md"), "Use Docker."),
            ),
            language_counts=(("python", 3),),
            docker_files=(Path("/project/Dockerfile"),),
            command_errors=(),
        ),
        work_items={"inspect": item},
        routes={"inspect": route},
        results={"inspect": result},
        approvals={},
        status=RunStatus.COMPLETED,
    )

    report = ReportRenderer().render(state)

    for expected in (
        "Understand the service",
        "/project",
        "AGENTS.md",
        "dirty.txt",
        "Inspect service",
        "matching rule 0",
        "example-model",
        "low",
        "read-only",
        "automatic",
        "succeeded",
        "thread-1",
        "10",
        "1.25",
        "src/service.py",
        "reviewed",
        "Total usage",
        "[REDACTED]",
    ):
        assert expected in report
    assert "sk-secret-value" not in report
    assert report == ReportRenderer().render(state)
