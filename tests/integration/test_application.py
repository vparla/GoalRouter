# SPDX-License-Identifier: MIT
# File: tests/integration/test_application.py
# Purpose: Verify application workflows across fake infrastructure ports

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from goalrouter.application import GoalRouterApplication
from goalrouter.approvals import ApprovalService
from goalrouter.config import RouterConfig, load_router_config
from goalrouter.domain import (
    AccessMode,
    InstructionFile,
    JsonValue,
    Objective,
    RepositoryContext,
    RouteDecision,
    RunEvent,
    RunState,
    RunStatus,
    WorkItem,
    WorkResult,
    WorkStatus,
)
from goalrouter.errors import ProjectBusyError, RunBusyError
from goalrouter.locking import ProjectWriteLeaseProtocol, RunLeaseProtocol
from goalrouter.reporting import ReportRenderer
from goalrouter.routing import TaskRouter
from goalrouter.scheduler import WorkScheduler

ROOT = Path(__file__).resolve().parents[2]


def _config() -> RouterConfig:
    return load_router_config(
        ROOT / "config/task-models.yaml",
        schema_path=ROOT / "config/task-models.schema.json",
    )


class FakeRepositoryInspector:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def inspect(self, project_path: Path) -> RepositoryContext:
        self.calls.append("inspect")
        return RepositoryContext(
            project_path=project_path,
            is_git_worktree=False,
            branch=None,
            dirty_paths=(),
            instruction_files=(
                InstructionFile(project_path / "AGENTS.md", "Use the declared lifecycle."),
            ),
            language_counts=(("python", 1),),
            docker_files=(),
            command_errors=(),
        )


class FakePlanner:
    def __init__(self, items: tuple[WorkItem, ...], calls: list[str]) -> None:
        self.items = items
        self.calls = calls
        self.projects: list[Path] = []

    async def plan(
        self,
        objective: Objective,
        repository: RepositoryContext,
        config: RouterConfig,
    ) -> tuple[WorkItem, ...]:
        del repository, config
        self.calls.append("plan")
        self.projects.append(objective.project_path)
        return self.items


class FakeCodexClient:
    def __init__(
        self,
        config: RouterConfig,
        calls: list[str],
        *,
        fail_completion: bool = False,
    ) -> None:
        self.config = config
        self.calls = calls
        self.fail_completion = fail_completion
        self.prompts: list[str] = []

    async def available_models(self) -> frozenset[str]:
        self.calls.append("models")
        return frozenset(alias.model for alias in self.config.model_aliases.values())

    async def run_new_thread(
        self,
        *,
        project_path: Path,
        route: RouteDecision,
        prompt: str,
        developer_instructions: str,
        output_schema: Mapping[str, object] | None = None,
    ) -> WorkResult:
        del project_path, developer_instructions, output_schema
        self.calls.append("run")
        self.prompts.append(prompt)
        if self.fail_completion and prompt.startswith("Independently verify"):
            raise RuntimeError("verification incomplete")
        return WorkResult(
            work_item_id=route.task,
            thread_id=f"thread-{len(self.prompts)}",
            turn_id=f"turn-{len(self.prompts)}",
            status=WorkStatus.SUCCEEDED,
            final_response="done",
            sdk_items=tuple[JsonValue](),
            input_tokens=2,
            cached_input_tokens=0,
            output_tokens=1,
            duration_seconds=0.25,
            changed_paths=(),
            verification=("verified",),
            confidence=1.0,
            escalation_requested=False,
            error=None,
        )

    async def resume_thread(
        self,
        *,
        thread_id: str,
        project_path: Path,
        route: RouteDecision,
        prompt: str,
        output_schema: Mapping[str, object] | None = None,
    ) -> WorkResult:
        del thread_id, project_path, route, prompt, output_schema
        raise AssertionError("application scheduler must not resume completed work")


class MemoryStore:
    def __init__(self, calls: list[str] | None = None) -> None:
        self.calls = calls
        self.states: dict[str, RunState] = {}
        self.persisted_states: dict[str, dict[str, JsonValue]] = {}
        self.save_count = 0
        self.events: list[RunEvent] = []
        self.reports: dict[str, str] = {}

    async def create(self, state: RunState) -> None:
        if self.calls is not None:
            self.calls.append("store-create")
        self.states[state.run_id] = state
        self.persisted_states[state.run_id] = state.to_dict()
        self.save_count += 1

    async def load(
        self,
        run_id: str,
        *,
        expected_configuration_digest: str | None = None,
        acknowledge_configuration_change: bool = False,
    ) -> RunState:
        del acknowledge_configuration_change
        if self.calls is not None:
            self.calls.append("store-load")
        state = self.states[run_id]
        if (
            expected_configuration_digest is not None
            and state.configuration_digest != expected_configuration_digest
        ):
            raise AssertionError("unexpected config change")
        return state

    async def save(self, state: RunState) -> None:
        if self.calls is not None:
            self.calls.append("state-save")
        self.states[state.run_id] = state
        self.persisted_states[state.run_id] = state.to_dict()
        self.save_count += 1

    async def append_event(self, run_id: str, event: RunEvent) -> None:
        assert run_id in self.states
        if self.calls is not None:
            self.calls.append("event-append")
        self.events.append(event)

    async def write_report(self, run_id: str, content: str) -> Path:
        if self.calls is not None:
            self.calls.append("report-write")
        self.reports[run_id] = content
        return Path("/state") / run_id / "report.md"


class NoOpRunLease:
    @asynccontextmanager
    async def acquire(self, run_id: str) -> AsyncIterator[None]:
        del run_id
        yield


class RecordingRunLease:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    @asynccontextmanager
    async def acquire(self, run_id: str) -> AsyncIterator[None]:
        self.calls.append(f"run-enter:{run_id}")
        try:
            yield
        finally:
            self.calls.append(f"run-exit:{run_id}")


class BusyRunLease:
    @asynccontextmanager
    async def acquire(self, run_id: str) -> AsyncIterator[None]:
        raise RunBusyError(f"Run is busy: {run_id}")
        yield


class SerialRunLease:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self, run_id: str) -> AsyncIterator[None]:
        await self._lock.acquire()
        self.calls.append(f"run-enter:{run_id}")
        try:
            yield
        finally:
            self.calls.append(f"run-exit:{run_id}")
            self._lock.release()


class NoOpProjectWriteLease:
    @asynccontextmanager
    async def acquire(self, project_path: Path) -> AsyncIterator[None]:
        del project_path
        yield


class MutableProjectWriteLease:
    def __init__(self) -> None:
        self.busy = False

    @asynccontextmanager
    async def acquire(self, project_path: Path) -> AsyncIterator[None]:
        if self.busy:
            raise ProjectBusyError(f"Project is busy: {project_path}")
        yield


class BlockingReportStore(MemoryStore):
    def __init__(self, calls: list[str]) -> None:
        super().__init__(calls)
        self.first_report_started = asyncio.Event()
        self.release_first_report = asyncio.Event()
        self.report_calls = 0

    async def write_report(self, run_id: str, content: str) -> Path:
        self.report_calls += 1
        if self.report_calls == 1:
            self.first_report_started.set()
            await self.release_first_report.wait()
        return await super().write_report(run_id, content)


def _application(
    *,
    items: tuple[WorkItem, ...],
    fail_completion: bool = False,
    run_lease: RunLeaseProtocol | None = None,
    project_write_lease: ProjectWriteLeaseProtocol | None = None,
    store: MemoryStore | None = None,
    calls: list[str] | None = None,
) -> tuple[GoalRouterApplication, FakeCodexClient, MemoryStore, FakePlanner, list[str]]:
    config = _config()
    selected_calls = calls if calls is not None else []
    client = FakeCodexClient(config, selected_calls, fail_completion=fail_completion)
    selected_store = store or MemoryStore(selected_calls)
    approvals = ApprovalService()
    planner = FakePlanner(items, selected_calls)
    router = TaskRouter(config)
    scheduler = WorkScheduler(
        client,
        selected_store,
        approvals,
        project_write_lease=project_write_lease or NoOpProjectWriteLease(),
        max_read_concurrency=config.maximum_read_concurrency,
    )
    application = GoalRouterApplication(
        config=config,
        config_path=ROOT / "config/task-models.yaml",
        client=client,
        repository=FakeRepositoryInspector(selected_calls),
        planner=planner,
        router=router,
        scheduler=scheduler,
        approvals=approvals,
        store=selected_store,
        reporter=ReportRenderer(),
        run_lease=run_lease or NoOpRunLease(),
        clock=lambda: datetime(2026, 8, 3, tzinfo=UTC),
        run_id_factory=lambda: "run-1",
    )
    return application, client, selected_store, planner, selected_calls


@pytest.mark.asyncio
async def test_explicit_task_inspects_before_models_then_executes_one_work_item(
    tmp_path: Path,
) -> None:
    application, client, store, _, calls = _application(items=())

    state = await application.run_task(
        project_path=tmp_path,
        task="repository-search",
        prompt="Find the service boundary",
    )

    assert calls[:2] == ["inspect", "models"]
    assert client.prompts == ["Find the service boundary"]
    assert tuple(state.work_items) == ("task",)
    assert state.status is RunStatus.COMPLETED
    assert store.save_count >= 3
    assert "run-1" in store.reports


def _objective_items() -> tuple[WorkItem, ...]:
    inspect = WorkItem(
        id="inspect",
        title="Inspect",
        instructions="Inspect",
        task="repository-search",
        phase="discovery",
        dependencies=(),
        access=AccessMode.READ_ONLY,
        affected_paths=(),
        expected_result="evidence",
        verification=(),
        confidence=1.0,
        risk_flags=frozenset(),
    )
    implement = WorkItem(
        id="implement",
        title="Implement",
        instructions="Implement",
        task="python-coding",
        phase="implementation",
        dependencies=("inspect",),
        access=AccessMode.WORKSPACE_WRITE,
        affected_paths=(Path("src/example.py"),),
        expected_result="working code",
        verification=("tests pass",),
        confidence=0.9,
        risk_flags=frozenset(),
    )
    return inspect, implement


@pytest.mark.asyncio
async def test_objective_pauses_for_approval_then_resumes_and_completes(
    tmp_path: Path,
) -> None:
    application, client, store, planner, calls = _application(items=_objective_items())

    paused = await application.run_objective(
        project_path=tmp_path,
        prompt="Implement the feature",
    )

    assert calls[:3] == ["inspect", "models", "plan"]
    assert planner.projects == [tmp_path]
    assert paused.status is RunStatus.AWAITING_APPROVAL
    assert tuple(paused.results) == ("inspect",)

    await application.approve(
        "run-1",
        "implement",
        approved_by="vinny@example.com",
    )
    completed = await application.resume("run-1")

    assert completed.status is RunStatus.COMPLETED
    assert "__goalrouter_completion_review__" in completed.work_items
    assert len(client.prompts) == 3
    assert any(event.event == "approval-recorded" for event in store.events)
    assert "Total usage" in store.reports["run-1"]


@pytest.mark.asyncio
async def test_failed_completion_review_prevents_completed_status(tmp_path: Path) -> None:
    read = _objective_items()[0]
    application, _, _, planner, _ = _application(
        items=(read,),
        fail_completion=True,
    )

    state = await application.run_objective(
        project_path=tmp_path,
        prompt="Inspect and verify",
    )

    assert planner.projects == [tmp_path]
    assert state.status is RunStatus.FAILED
    assert state.results["__goalrouter_completion_review__"].status is WorkStatus.FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("plan", "task", "objective"))
async def test_new_run_operations_hold_one_run_lease_from_before_create(
    tmp_path: Path,
    operation: str,
) -> None:
    calls: list[str] = []
    lease = RecordingRunLease(calls)
    application, _, _, _, _ = _application(
        items=() if operation != "objective" else (_objective_items()[0],),
        run_lease=lease,
        calls=calls,
    )

    if operation == "plan":
        await application.plan_objective(
            project_path=tmp_path,
            prompt="Plan safely",
            run_id="selected-run",
        )
    elif operation == "task":
        await application.run_task(
            project_path=tmp_path,
            task="repository-search",
            prompt="Run safely",
            run_id="selected-run",
        )
    else:
        await application.run_objective(
            project_path=tmp_path,
            prompt="Run objective safely",
            run_id="selected-run",
        )

    assert calls[0] == "run-enter:selected-run"
    assert calls[-1] == "run-exit:selected-run"
    assert calls.count("run-enter:selected-run") == 1
    assert calls.count("run-exit:selected-run") == 1
    assert calls.index("run-enter:selected-run") < calls.index("store-create")
    if operation != "plan":
        assert calls.index("report-write") < calls.index("run-exit:selected-run")


@pytest.mark.asyncio
async def test_existing_run_mutations_hold_lease_before_load_and_status_does_not(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    lease = RecordingRunLease(calls)
    application, _, _, _, _ = _application(
        items=_objective_items(),
        run_lease=lease,
        calls=calls,
    )
    await application.plan_objective(
        project_path=tmp_path,
        prompt="Plan safely",
        run_id="run-1",
    )

    calls.clear()
    await application.approve("run-1", "implement", approved_by="operator")
    assert calls == [
        "run-enter:run-1",
        "store-load",
        "state-save",
        "event-append",
        "report-write",
        "run-exit:run-1",
    ]

    calls.clear()
    await application.report("run-1")
    assert calls == [
        "run-enter:run-1",
        "store-load",
        "report-write",
        "run-exit:run-1",
    ]

    calls.clear()
    await application.status("run-1")
    assert calls == ["store-load"]

    calls.clear()
    await application.resume("run-1")
    assert calls[0:2] == ["run-enter:run-1", "store-load"]
    assert calls[-2:] == ["report-write", "run-exit:run-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("plan", "resume"))
async def test_run_contention_fails_before_create_or_load(
    tmp_path: Path,
    operation: str,
) -> None:
    calls: list[str] = []
    application, client, store, _, _ = _application(
        items=(),
        run_lease=BusyRunLease(),
        calls=calls,
    )

    with pytest.raises(RunBusyError, match="Run is busy"):
        if operation == "plan":
            await application.plan_objective(
                project_path=tmp_path,
                prompt="Do not inspect",
                run_id="run-1",
            )
        else:
            await application.resume("run-1")

    assert calls == []
    assert client.prompts == []
    assert store.states == {}


@pytest.mark.asyncio
async def test_second_resume_cannot_load_until_first_run_lease_exits(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    store = BlockingReportStore(calls)
    application, _, _, _, _ = _application(
        items=(),
        run_lease=SerialRunLease(calls),
        store=store,
        calls=calls,
    )
    await application.plan_objective(
        project_path=tmp_path,
        prompt="Seed run",
        run_id="run-1",
    )
    calls.clear()

    async with asyncio.timeout(2):
        async with asyncio.TaskGroup() as group:
            first = group.create_task(application.resume("run-1"))
            await store.first_report_started.wait()
            second = group.create_task(application.resume("run-1"))
            await asyncio.sleep(0)
            assert calls.count("run-enter:run-1") == 1
            assert calls.count("store-load") == 1
            store.release_first_report.set()

    assert first.result().run_id == "run-1"
    assert second.result().run_id == "run-1"
    first_exit = calls.index("run-exit:run-1")
    second_enter = calls.index("run-enter:run-1", first_exit + 1)
    second_load = calls.index("store-load", second_enter + 1)
    assert first_exit < second_enter < second_load


@pytest.mark.asyncio
async def test_approved_writer_busy_resume_is_planned_and_retries_without_reapproval(
    tmp_path: Path,
) -> None:
    project_lease = MutableProjectWriteLease()
    application, client, store, _, _ = _application(
        items=_objective_items(),
        project_write_lease=project_lease,
    )
    paused = await application.run_objective(
        project_path=tmp_path,
        prompt="Implement safely",
        run_id="run-1",
    )
    assert paused.status is RunStatus.AWAITING_APPROVAL
    assert paused.results["inspect"].status is WorkStatus.SUCCEEDED

    approved = await application.approve(
        "run-1",
        "implement",
        approved_by="operator",
    )
    approval = approved.approvals["implement"]
    prompts_before_busy = list(client.prompts)
    events_before_busy = list(store.events)
    report_before_busy = store.reports["run-1"]
    project_lease.busy = True

    with pytest.raises(ProjectBusyError, match="Project is busy"):
        await application.resume("run-1")

    busy_state = store.states["run-1"]
    assert busy_state.status is RunStatus.PLANNED
    assert busy_state.approvals["implement"] == approval
    assert busy_state.results["inspect"].status is WorkStatus.SUCCEEDED
    assert "implement" not in busy_state.results
    assert client.prompts == prompts_before_busy
    assert store.events == events_before_busy
    assert store.reports["run-1"] == report_before_busy
    persisted = store.persisted_states["run-1"]
    assert persisted["status"] == "planned"
    assert set(persisted["approvals"]) == {"implement"}
    assert set(persisted["results"]) == {"inspect"}

    project_lease.busy = False
    completed = await application.resume("run-1")

    assert completed.status is RunStatus.COMPLETED
    assert completed.approvals["implement"] == approval
    assert completed.results["implement"].status is WorkStatus.SUCCEEDED
    assert len(client.prompts) == 3
    assert sum(event.event == "approval-recorded" for event in store.events) == 1
