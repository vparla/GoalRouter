# SPDX-License-Identifier: MIT
# File: tests/unit/test_scheduler.py
# Purpose: Verify dependency, concurrency, approval, and persistence scheduling

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from goalrouter.approvals import ApprovalService
from goalrouter.domain import (
    AccessMode,
    ApprovalMode,
    JsonValue,
    Objective,
    RepositoryContext,
    RouteDecision,
    RouteSource,
    RunEvent,
    RunState,
    RunStatus,
    SandboxMode,
    WorkItem,
    WorkResult,
    WorkStatus,
)
from goalrouter.errors import ProjectBusyError
from goalrouter.scheduler import WorkScheduler


def _item(
    item_id: str,
    *,
    dependencies: tuple[str, ...] = (),
    access: AccessMode = AccessMode.READ_ONLY,
) -> WorkItem:
    return WorkItem(
        id=item_id,
        title=item_id,
        instructions=item_id,
        task="implementation" if access is AccessMode.WORKSPACE_WRITE else "analysis",
        phase="work",
        dependencies=dependencies,
        access=access,
        affected_paths=(),
        expected_result="done",
        verification=(),
        confidence=1.0,
        risk_flags=frozenset(),
    )


def _route(
    item: WorkItem,
    *,
    approval: ApprovalMode = ApprovalMode.AUTOMATIC,
) -> RouteDecision:
    return RouteDecision(
        task=item.task,
        model_alias="economy",
        model="example-model",
        reasoning_effort="low",
        sandbox=(
            SandboxMode.WORKSPACE_WRITE
            if item.access is AccessMode.WORKSPACE_WRITE
            else SandboxMode.READ_ONLY
        ),
        approval=approval,
        timeout_seconds=30,
        max_attempts=1,
        destructive=False,
        external_write=False,
        escalation_task=None,
        source=RouteSource.PLANNER,
        reason="planned",
    )


def _state(
    *items: WorkItem,
    approvals: dict[str, ApprovalMode] | None = None,
) -> RunState:
    approval_modes = approvals or {}
    return RunState(
        schema_version=1,
        configuration_digest="digest",
        objective=Objective(
            id="run-1",
            prompt="objective",
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
        work_items={item.id: item for item in items},
        routes={
            item.id: _route(item, approval=approval_modes.get(item.id, ApprovalMode.AUTOMATIC))
            for item in items
        },
        results={},
        approvals={},
        status=RunStatus.PLANNED,
    )


class MemoryStore:
    def __init__(self, calls: list[str] | None = None) -> None:
        self.calls = calls
        self.saved_result_ids: list[tuple[str, ...]] = []
        self.saved_statuses: list[RunStatus] = []
        self.events: list[RunEvent] = []

    async def save(self, state: RunState) -> None:
        if self.calls is not None:
            self.calls.append("state-save")
        self.saved_result_ids.append(tuple(sorted(state.results)))
        self.saved_statuses.append(state.status)

    async def append_event(self, run_id: str, event: RunEvent) -> None:
        assert run_id == "run-1"
        if self.calls is not None:
            self.calls.append("event-append")
        self.events.append(event)


class InstrumentedClient:
    def __init__(
        self,
        *,
        fail: frozenset[str] = frozenset(),
        calls: list[str] | None = None,
    ) -> None:
        self.fail = fail
        self.calls = calls
        self.starts: list[str] = []
        self.active_reads = 0
        self.active_writers = 0
        self.max_reads = 0
        self.overlap_violation = False
        self.resume_starts: list[str] = []

    async def run_new_thread(
        self,
        *,
        project_path: Path,
        route: RouteDecision,
        prompt: str,
        developer_instructions: str,
        output_schema: object = None,
    ) -> WorkResult:
        del project_path, developer_instructions, output_schema
        if self.calls is not None:
            self.calls.append("sdk-dispatch")
        self.starts.append(prompt)
        writer = route.sandbox is SandboxMode.WORKSPACE_WRITE
        if writer:
            self.overlap_violation |= self.active_reads > 0 or self.active_writers > 0
            self.active_writers += 1
        else:
            self.overlap_violation |= self.active_writers > 0
            self.active_reads += 1
            self.max_reads = max(self.max_reads, self.active_reads)
        try:
            if prompt in self.fail:
                await asyncio.sleep(0)
                raise RuntimeError(f"failure-{prompt}")
            await asyncio.sleep(0.01)
            return WorkResult(
                work_item_id=route.task,
                thread_id=f"thread-{prompt}",
                turn_id=f"turn-{prompt}",
                status=WorkStatus.SUCCEEDED,
                final_response="done",
                sdk_items=tuple[JsonValue](),
                input_tokens=1,
                cached_input_tokens=0,
                output_tokens=1,
                duration_seconds=0.01,
                changed_paths=(),
                verification=(),
                confidence=1.0,
                escalation_requested=False,
                error=None,
            )
        finally:
            if writer:
                self.active_writers -= 1
            else:
                self.active_reads -= 1

    async def resume_thread(
        self,
        *,
        thread_id: str,
        project_path: Path,
        route: RouteDecision,
        prompt: str,
        output_schema: object = None,
    ) -> WorkResult:
        self.resume_starts.append(thread_id)
        return await self.run_new_thread(
            project_path=project_path,
            route=route,
            prompt=prompt,
            developer_instructions="",
            output_schema=output_schema,
        )


class NoOpProjectWriteLease:
    @asynccontextmanager
    async def acquire(self, project_path: Path) -> AsyncIterator[None]:
        del project_path
        yield


class RecordingProjectWriteLease:
    def __init__(self, calls: list[str], state: RunState) -> None:
        self.calls = calls
        self.state = state

    @asynccontextmanager
    async def acquire(self, project_path: Path) -> AsyncIterator[None]:
        assert project_path == self.state.objective.project_path
        self.calls.append(f"project-enter:{self.state.status.value}")
        try:
            yield
        finally:
            self.calls.append("project-exit")


class BusyProjectWriteLease:
    @asynccontextmanager
    async def acquire(self, project_path: Path) -> AsyncIterator[None]:
        raise ProjectBusyError(f"Project is busy: {project_path}")
        yield


class MutableProjectWriteLease:
    def __init__(self, *, busy: bool) -> None:
        self.busy = busy

    @asynccontextmanager
    async def acquire(self, project_path: Path) -> AsyncIterator[None]:
        if self.busy:
            raise ProjectBusyError(f"Project is busy: {project_path}")
        yield


class RecordingApprovalService(ApprovalService):
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.checks = 0

    def require_dispatchable(self, state: RunState, work_item_id: str) -> None:
        self.checks += 1
        self.calls.append(
            "approval-check" if self.checks == 1 else "approval-revalidate"
        )
        super().require_dispatchable(state, work_item_id)


@pytest.mark.asyncio
async def test_dependencies_run_in_order_and_completed_work_is_not_rerun() -> None:
    first = _item("first")
    second = _item("second", dependencies=("first",))
    state = _state(first, second)
    client = InstrumentedClient()
    scheduler = WorkScheduler(
        client,
        MemoryStore(),
        ApprovalService(),
        project_write_lease=NoOpProjectWriteLease(),
        max_read_concurrency=2,
    )

    await scheduler.run_ready(state)
    await scheduler.run_ready(state)

    assert client.starts == ["first", "second"]
    assert state.status is RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_reads_are_bounded_and_a_writer_always_runs_alone() -> None:
    writer = _item("writer", access=AccessMode.WORKSPACE_WRITE)
    state = _state(writer, _item("read-a"), _item("read-b"), _item("read-c"))
    client = InstrumentedClient()
    scheduler = WorkScheduler(
        client,
        MemoryStore(),
        ApprovalService(),
        project_write_lease=NoOpProjectWriteLease(),
        max_read_concurrency=2,
    )

    await scheduler.run_ready(state)

    assert client.starts[0] == "writer"
    assert client.max_reads == 2
    assert not client.overlap_violation
    assert state.status is RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_failure_cancels_and_records_sibling_then_blocks_dependents() -> None:
    failed = _item("failed")
    sibling = _item("sibling")
    dependent = _item("dependent", dependencies=("failed",))
    state = _state(failed, sibling, dependent)
    store = MemoryStore()
    scheduler = WorkScheduler(
        InstrumentedClient(fail=frozenset({"failed"})),
        store,
        ApprovalService(),
        project_write_lease=NoOpProjectWriteLease(),
        max_read_concurrency=2,
    )

    await scheduler.run_ready(state)

    assert state.results["failed"].status is WorkStatus.FAILED
    assert state.results["sibling"].status is WorkStatus.FAILED
    assert "cancel" in (state.results["sibling"].error or "").casefold()
    assert state.results["dependent"].status is WorkStatus.BLOCKED
    assert state.status is RunStatus.FAILED
    assert any("failed" in saved for saved in store.saved_result_ids)


@pytest.mark.asyncio
async def test_required_approval_stays_pending_while_automatic_work_runs() -> None:
    required = _item("required", access=AccessMode.WORKSPACE_WRITE)
    automatic = _item("automatic")
    state = _state(
        required,
        automatic,
        approvals={"required": ApprovalMode.REQUIRED},
    )
    client = InstrumentedClient()
    scheduler = WorkScheduler(
        client,
        MemoryStore(),
        ApprovalService(),
        project_write_lease=NoOpProjectWriteLease(),
        max_read_concurrency=2,
    )

    await scheduler.run_ready(state)

    assert client.starts == ["automatic"]
    assert "required" not in state.results
    assert state.status is RunStatus.AWAITING_APPROVAL


@pytest.mark.asyncio
async def test_interrupted_running_item_resumes_its_stored_thread() -> None:
    item = _item("interrupted")
    state = _state(item)
    state.results[item.id] = WorkResult(
        work_item_id=item.id,
        thread_id="stored-thread",
        turn_id=None,
        status=WorkStatus.RUNNING,
        final_response=None,
        sdk_items=tuple[JsonValue](),
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=0,
        duration_seconds=0,
        changed_paths=(),
        verification=(),
        confidence=0,
        escalation_requested=False,
        error=None,
    )
    client = InstrumentedClient()
    scheduler = WorkScheduler(
        client,
        MemoryStore(),
        ApprovalService(),
        project_write_lease=NoOpProjectWriteLease(),
        max_read_concurrency=1,
    )

    await scheduler.run_ready(state)

    assert client.resume_starts == ["stored-thread"]
    assert state.results[item.id].status is WorkStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_writer_lease_precedes_revalidation_dispatch_and_persistence() -> None:
    calls: list[str] = []
    writer = _item("writer", access=AccessMode.WORKSPACE_WRITE)
    state = _state(writer)
    approvals = RecordingApprovalService(calls)
    scheduler = WorkScheduler(
        InstrumentedClient(calls=calls),
        MemoryStore(calls),
        approvals,
        project_write_lease=RecordingProjectWriteLease(calls, state),
        max_read_concurrency=1,
    )

    await scheduler.run_ready(state)

    assert calls[:7] == [
        "approval-check",
        "project-enter:planned",
        "approval-revalidate",
        "sdk-dispatch",
        "state-save",
        "event-append",
        "project-exit",
    ]
    assert state.status is RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_project_contention_is_retryable_without_dispatch_or_failure_result() -> None:
    writer = _item("writer", access=AccessMode.WORKSPACE_WRITE)
    state = _state(writer)
    client = InstrumentedClient()
    store = MemoryStore()
    scheduler = WorkScheduler(
        client,
        store,
        ApprovalService(),
        project_write_lease=BusyProjectWriteLease(),
        max_read_concurrency=1,
    )

    with pytest.raises(ProjectBusyError, match="Project is busy"):
        await scheduler.run_ready(state)

    assert state.status is RunStatus.PLANNED
    assert state.results == {}
    assert client.starts == []
    assert store.saved_result_ids == []
    assert store.events == []


@pytest.mark.asyncio
async def test_read_only_batch_does_not_acquire_project_lease() -> None:
    item = _item("reader")
    state = _state(item)
    client = InstrumentedClient()
    scheduler = WorkScheduler(
        client,
        MemoryStore(),
        ApprovalService(),
        project_write_lease=BusyProjectWriteLease(),
        max_read_concurrency=1,
    )

    await scheduler.run_ready(state)

    assert client.starts == ["reader"]
    assert state.status is RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_writer_busy_after_reader_batch_leaves_resumable_planned_checkpoint() -> None:
    reader = _item("reader")
    writer = _item(
        "writer",
        dependencies=("reader",),
        access=AccessMode.WORKSPACE_WRITE,
    )
    state = _state(reader, writer)
    client = InstrumentedClient()
    store = MemoryStore()
    lease = MutableProjectWriteLease(busy=True)
    scheduler = WorkScheduler(
        client,
        store,
        ApprovalService(),
        project_write_lease=lease,
        max_read_concurrency=1,
    )

    with pytest.raises(ProjectBusyError, match="Project is busy"):
        await scheduler.run_ready(state)

    assert state.status is RunStatus.PLANNED
    assert store.saved_statuses[-1] is RunStatus.PLANNED
    assert state.results["reader"].status is WorkStatus.SUCCEEDED
    assert "writer" not in state.results
    assert client.starts == ["reader"]

    lease.busy = False
    await scheduler.run_ready(state)

    assert client.starts == ["reader", "writer"]
    assert state.results["writer"].status is WorkStatus.SUCCEEDED
    assert state.status is RunStatus.COMPLETED
