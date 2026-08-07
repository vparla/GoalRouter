# SPDX-License-Identifier: MIT
# File: src/goalrouter/application.py
# Purpose: Coordinate routing, planning, execution, approval, resume, and reports

"""Application use cases composed entirely from SDK-independent ports."""

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from goalrouter.approvals import ApprovalService
from goalrouter.config import RouterConfig
from goalrouter.domain import (
    AccessMode,
    JsonValue,
    Objective,
    RouteDecision,
    RunEvent,
    RunState,
    RunStatus,
    SandboxMode,
    WorkItem,
    WorkStatus,
)
from goalrouter.errors import PlannerOutputError
from goalrouter.locking import RunLeaseProtocol
from goalrouter.planner import PlannerProtocol
from goalrouter.reporting import ReportRendererProtocol
from goalrouter.repository import RepositoryInspectorProtocol
from goalrouter.routing import TaskRouter
from goalrouter.scheduler import WorkSchedulerProtocol
from goalrouter.sdk.protocol import CodexClientProtocol
from goalrouter.storage.protocol import RunStoreProtocol

_COMPLETION_ID = "__goalrouter_completion_review__"


class GoalRouterApplication:
    """Coordinate complete GoalRouter workflows without CLI or SDK coupling."""

    def __init__(
        self,
        *,
        config: RouterConfig,
        config_path: Path,
        client: CodexClientProtocol,
        repository: RepositoryInspectorProtocol,
        planner: PlannerProtocol,
        router: TaskRouter,
        scheduler: WorkSchedulerProtocol,
        approvals: ApprovalService,
        store: RunStoreProtocol,
        reporter: ReportRendererProtocol,
        run_lease: RunLeaseProtocol,
        clock: Callable[[], datetime] | None = None,
        run_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._config = config
        self._config_path = config_path
        self._client = client
        self._repository = repository
        self._planner = planner
        self._router = router
        self._scheduler = scheduler
        self._approvals = approvals
        self._store = store
        self._reporter = reporter
        self._run_lease = run_lease
        self._clock = clock or (lambda: datetime.now(UTC))
        self._run_id_factory = run_id_factory or (lambda: uuid4().hex)

    async def models(self) -> frozenset[str]:
        """Return the validated account model inventory."""

        models = await self._client.available_models()
        self._router.validate_models(models)
        return models

    async def route_task(
        self,
        *,
        project_path: Path,
        task: str,
        prompt: str,
        affected_paths: Sequence[Path] = (),
    ) -> RouteDecision:
        """Resolve and validate one explicit route without dispatching it."""

        await self._validate_model_inventory()
        item, route = self._explicit_item(
            task=task,
            prompt=prompt,
            affected_paths=affected_paths,
        )
        del project_path, item
        return route

    async def plan_objective(
        self,
        *,
        project_path: Path,
        prompt: str,
        run_id: str | None = None,
    ) -> RunState:
        """Discover and persist a validated routed objective plan."""

        selected_run_id = run_id or self._run_id_factory()
        async with self._run_lease.acquire(selected_run_id):
            return await self._plan_objective_unlocked(
                project_path=project_path,
                prompt=prompt,
                run_id=selected_run_id,
            )

    async def _plan_objective_unlocked(
        self,
        *,
        project_path: Path,
        prompt: str,
        run_id: str,
    ) -> RunState:
        repository = await self._repository.inspect(project_path)
        await self._validate_model_inventory()
        objective = self._objective(
            project_path=repository.project_path,
            prompt=prompt,
            explicit_task=None,
            run_id=run_id,
        )
        items = await self._planner.plan(objective, repository, self._config)
        if any(item.id == _COMPLETION_ID for item in items):
            raise PlannerOutputError(f"Planner used reserved work-item ID {_COMPLETION_ID}")
        state = RunState(
            schema_version=1,
            configuration_digest=self._config.digest,
            objective=objective,
            repository=repository,
            work_items={item.id: item for item in items},
            routes={item.id: self._router.route(item) for item in items},
            results={},
            approvals={},
            status=RunStatus.PLANNED,
        )
        await self._store.create(state)
        await self._event(state, "objective-planned")
        return state

    async def run_task(
        self,
        *,
        project_path: Path,
        task: str,
        prompt: str,
        affected_paths: Sequence[Path] = (),
        run_id: str | None = None,
    ) -> RunState:
        """Create, execute, checkpoint, and report one explicit bounded task."""

        selected_run_id = run_id or self._run_id_factory()
        async with self._run_lease.acquire(selected_run_id):
            repository = await self._repository.inspect(project_path)
            await self._validate_model_inventory()
            item, route = self._explicit_item(
                task=task,
                prompt=prompt,
                affected_paths=affected_paths,
            )
            objective = self._objective(
                project_path=repository.project_path,
                prompt=prompt,
                explicit_task=task,
                run_id=selected_run_id,
            )
            state = RunState(
                schema_version=1,
                configuration_digest=self._config.digest,
                objective=objective,
                repository=repository,
                work_items={item.id: item},
                routes={item.id: route},
                results={},
                approvals={},
                status=RunStatus.PLANNED,
            )
            await self._store.create(state)
            await self._event(state, "task-created")
            await self._scheduler.run_ready(state)
            await self._write_report(state)
            return state

    async def run_objective(
        self,
        *,
        project_path: Path,
        prompt: str,
        run_id: str | None = None,
    ) -> RunState:
        """Plan and execute an objective until completion or a safe pause."""

        selected_run_id = run_id or self._run_id_factory()
        async with self._run_lease.acquire(selected_run_id):
            state = await self._plan_objective_unlocked(
                project_path=project_path,
                prompt=prompt,
                run_id=selected_run_id,
            )
            return await self._continue(state)

    async def approve(
        self,
        run_id: str,
        work_item_id: str,
        *,
        approved_by: str,
    ) -> RunState:
        """Persist approval for the exact current dispatch fingerprint."""

        async with self._run_lease.acquire(run_id):
            state = await self._store.load(
                run_id,
                expected_configuration_digest=self._config.digest,
            )
            self._approvals.approve(
                state,
                work_item_id=work_item_id,
                approved_by=approved_by,
                approved_at=self._clock(),
            )
            await self._store.save(state)
            await self._event(
                state,
                "approval-recorded",
                work_item_id=work_item_id,
                details={"approved_by": approved_by},
            )
            await self._write_report(state)
            return state

    async def resume(
        self,
        run_id: str,
        *,
        acknowledge_configuration_change: bool = False,
    ) -> RunState:
        """Reload and continue only unfinished work under validated configuration."""

        async with self._run_lease.acquire(run_id):
            state = await self._store.load(
                run_id,
                expected_configuration_digest=self._config.digest,
                acknowledge_configuration_change=acknowledge_configuration_change,
            )
            if state.configuration_digest != self._config.digest:
                self._adopt_current_configuration(state)
                await self._store.save(state)
                await self._event(state, "configuration-change-acknowledged")
            await self._validate_model_inventory()
            return await self._continue(state)

    async def status(self, run_id: str) -> RunState:
        """Load the latest persisted run snapshot without mutation."""

        return await self._store.load(run_id)

    async def report(self, run_id: str) -> str:
        """Render and persist the latest report for a run."""

        async with self._run_lease.acquire(run_id):
            state = await self._store.load(run_id)
            return await self._write_report(state)

    async def _continue(self, state: RunState) -> RunState:
        await self._scheduler.run_ready(state)
        if state.objective.explicit_task is None and self._base_work_succeeded(state):
            if _COMPLETION_ID not in state.work_items:
                completion = _completion_item(state, self._config.completion_task)
                state.work_items[completion.id] = completion
                state.routes[completion.id] = self._router.route(completion)
                state.status = RunStatus.PLANNED
                await self._store.save(state)
                await self._event(state, "completion-review-appended")
            if _COMPLETION_ID not in state.results:
                await self._scheduler.run_ready(state)
        await self._write_report(state)
        return state

    async def _validate_model_inventory(self) -> None:
        self._router.validate_models(await self._client.available_models())

    def _explicit_item(
        self,
        *,
        task: str,
        prompt: str,
        affected_paths: Sequence[Path],
    ) -> tuple[WorkItem, RouteDecision]:
        provisional = WorkItem(
            id="task",
            title=f"Run {task}",
            instructions=prompt,
            task=task,
            phase="explicit",
            dependencies=(),
            access=AccessMode.READ_ONLY,
            affected_paths=tuple(affected_paths),
            expected_result="The requested bounded task is complete.",
            verification=(),
            confidence=1.0,
            risk_flags=frozenset(),
        )
        route = self._router.route(provisional, explicit_task=task)
        access = (
            AccessMode.WORKSPACE_WRITE
            if route.sandbox is SandboxMode.WORKSPACE_WRITE
            else AccessMode.READ_ONLY
        )
        return replace(provisional, access=access), route

    def _objective(
        self,
        *,
        project_path: Path,
        prompt: str,
        explicit_task: str | None,
        run_id: str,
    ) -> Objective:
        return Objective(
            id=run_id,
            prompt=prompt,
            project_path=project_path,
            explicit_task=explicit_task,
            config_path=self._config_path,
            created_at=self._clock(),
        )

    def _base_work_succeeded(self, state: RunState) -> bool:
        base_ids = [item_id for item_id in state.work_items if item_id != _COMPLETION_ID]
        return bool(base_ids) and all(
            (result := state.results.get(item_id)) is not None
            and result.status is WorkStatus.SUCCEEDED
            for item_id in base_ids
        )

    def _adopt_current_configuration(self, state: RunState) -> None:
        for item_id, item in state.work_items.items():
            result = state.results.get(item_id)
            if result is None or result.status in {
                WorkStatus.PENDING,
                WorkStatus.AWAITING_APPROVAL,
                WorkStatus.RUNNING,
            }:
                state.routes[item_id] = self._router.route(item)
        state.configuration_digest = self._config.digest
        state.approvals.clear()

    async def _event(
        self,
        state: RunState,
        event: str,
        *,
        work_item_id: str | None = None,
        details: dict[str, JsonValue] | None = None,
    ) -> None:
        await self._store.append_event(
            state.run_id,
            RunEvent(
                timestamp=self._clock(),
                event=event,
                work_item_id=work_item_id,
                details=details or {},
            ),
        )

    async def _write_report(self, state: RunState) -> str:
        content = self._reporter.render(state)
        await self._store.write_report(state.run_id, content)
        return content


def _completion_item(state: RunState, task: str) -> WorkItem:
    summaries = "\n".join(
        f"- {item_id}: {state.results[item_id].final_response or '(no response)'}"
        for item_id in sorted(state.results)
    )
    return WorkItem(
        id=_COMPLETION_ID,
        title="Independent completion review",
        instructions=(
            "Independently verify that the objective scope and required evidence are complete. "
            "Do not modify files. Report any missing verification explicitly.\n\n"
            f"Objective: {state.objective.prompt}\n\nCompleted work:\n{summaries}"
        ),
        task=task,
        phase="completion",
        dependencies=tuple(sorted(state.work_items)),
        access=AccessMode.READ_ONLY,
        affected_paths=(),
        expected_result="An independent completion decision with evidence.",
        verification=(),
        confidence=1.0,
        risk_flags=frozenset(),
    )
