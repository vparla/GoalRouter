# SPDX-License-Identifier: MIT
# File: src/goalrouter/scheduler.py
# Purpose: Dependency-aware, bounded async scheduling with serialized writes

"""Execute ready work while preserving dependency and write-safety invariants."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol

from goalrouter.approvals import ApprovalService
from goalrouter.domain import (
    AccessMode,
    JsonValue,
    RunEvent,
    RunState,
    RunStatus,
    WorkItem,
    WorkResult,
    WorkStatus,
)
from goalrouter.errors import ApprovalRequiredError, ConfigurationError
from goalrouter.locking import ProjectWriteLeaseProtocol
from goalrouter.sdk.protocol import CodexClientProtocol
from goalrouter.storage.protocol import RunStoreProtocol

_DEPENDENCY_FAILURES = frozenset({WorkStatus.FAILED, WorkStatus.BLOCKED})
_RESUMABLE_STATUSES = frozenset(
    {WorkStatus.PENDING, WorkStatus.AWAITING_APPROVAL, WorkStatus.RUNNING}
)
_PRE_WRITER_STATUSES = frozenset(
    {RunStatus.AWAITING_APPROVAL, RunStatus.RUNNING}
)


class WorkSchedulerProtocol(Protocol):
    """Port for dependency-ready execution used by the application coordinator."""

    async def run_ready(self, state: RunState) -> RunState: ...


class WorkScheduler:
    """Run dependency-ready work in safe, bounded batches."""

    def __init__(
        self,
        client: CodexClientProtocol,
        store: RunStoreProtocol,
        approvals: ApprovalService,
        *,
        project_write_lease: ProjectWriteLeaseProtocol,
        max_read_concurrency: int,
    ) -> None:
        if max_read_concurrency < 1:
            raise ConfigurationError("maximum read concurrency must be at least one")
        self._client = client
        self._store = store
        self._approvals = approvals
        self._project_write_lease = project_write_lease
        self._max_read_concurrency = max_read_concurrency

    async def run_ready(self, state: RunState) -> RunState:
        """Run all currently reachable batches and checkpoint every terminal result."""

        for _ in range(len(state.work_items)):
            blocked = self._newly_blocked(state)
            for item in blocked:
                await self._persist_result(
                    state,
                    _failure_result(
                        item,
                        status=WorkStatus.BLOCKED,
                        message="A dependency did not succeed",
                    ),
                )

            ready = self._dispatchable(state)
            if not ready:
                break

            writers = [item for item in ready if item.access is AccessMode.WORKSPACE_WRITE]
            if writers:
                item = writers[0]
                if state.status in _PRE_WRITER_STATUSES:
                    state.status = RunStatus.PLANNED
                    await self._store.save(state)
                async with self._project_write_lease.acquire(
                    state.objective.project_path
                ):
                    self._approvals.require_dispatchable(state, item.id)
                    state.status = RunStatus.RUNNING
                    result = await self._run_one(state, item)
                    await self._persist_result(state, result)
                continue

            state.status = RunStatus.RUNNING
            batch = ready[: self._max_read_concurrency]
            for result in await self._run_read_batch(state, batch):
                await self._persist_result(state, result)

        final_status = self._final_status(state)
        if state.status is not final_status:
            state.status = final_status
            await self._store.save(state)
        return state

    def _newly_blocked(self, state: RunState) -> list[WorkItem]:
        blocked: list[WorkItem] = []
        for item in _pending_items(state):
            dependency_results = [state.results.get(item_id) for item_id in item.dependencies]
            if any(
                result is not None and result.status in _DEPENDENCY_FAILURES
                for result in dependency_results
            ):
                blocked.append(item)
        return blocked

    def _dispatchable(self, state: RunState) -> list[WorkItem]:
        ready: list[WorkItem] = []
        for item in _pending_items(state):
            if not all(
                (result := state.results.get(dependency)) is not None
                and result.status is WorkStatus.SUCCEEDED
                for dependency in item.dependencies
            ):
                continue
            try:
                self._approvals.require_dispatchable(state, item.id)
            except ApprovalRequiredError:
                continue
            ready.append(item)
        return ready

    async def _run_one(self, state: RunState, item: WorkItem) -> WorkResult:
        try:
            return await self._dispatch(state, item)
        except Exception as error:
            return _failure_result(
                item,
                status=WorkStatus.FAILED,
                message=f"{type(error).__name__}: {error}",
            )

    async def _run_read_batch(
        self,
        state: RunState,
        items: list[WorkItem],
    ) -> list[WorkResult]:
        results: dict[str, WorkResult] = {}

        async def execute(item: WorkItem) -> None:
            try:
                results[item.id] = await self._dispatch(state, item)
            except asyncio.CancelledError:
                results[item.id] = _failure_result(
                    item,
                    status=WorkStatus.FAILED,
                    message="Cancelled because a sibling task failed",
                )
                raise
            except Exception as error:
                results[item.id] = _failure_result(
                    item,
                    status=WorkStatus.FAILED,
                    message=f"{type(error).__name__}: {error}",
                )
                raise

        try:
            async with asyncio.TaskGroup() as group:
                for item in items:
                    group.create_task(execute(item))
        except* Exception:
            pass
        return [results[item.id] for item in items]

    async def _dispatch(self, state: RunState, item: WorkItem) -> WorkResult:
        previous = state.results.get(item.id)
        if previous is not None and previous.thread_id is not None:
            result = await self._client.resume_thread(
                thread_id=previous.thread_id,
                project_path=state.objective.project_path,
                route=state.routes[item.id],
                prompt=item.instructions,
            )
        else:
            result = await self._client.run_new_thread(
                project_path=state.objective.project_path,
                route=state.routes[item.id],
                prompt=item.instructions,
                developer_instructions=_developer_instructions(state),
            )
        return replace(result, work_item_id=item.id)

    async def _persist_result(self, state: RunState, result: WorkResult) -> None:
        state.results[result.work_item_id] = result
        await self._store.save(state)
        await self._store.append_event(
            state.run_id,
            RunEvent(
                timestamp=datetime.now(UTC),
                event="work-item-finished",
                work_item_id=result.work_item_id,
                details={"status": result.status.value},
            ),
        )

    def _final_status(self, state: RunState) -> RunStatus:
        statuses = [result.status for result in state.results.values()]
        if any(status is WorkStatus.FAILED for status in statuses):
            return RunStatus.FAILED
        if any(status is WorkStatus.BLOCKED for status in statuses):
            return RunStatus.BLOCKED
        if len(state.results) == len(state.work_items) and all(
            status is WorkStatus.SUCCEEDED for status in statuses
        ):
            return RunStatus.COMPLETED
        if any(
            item.id not in state.results
            and state.routes[item.id].approval.value == "required"
            for item in state.work_items.values()
        ):
            return RunStatus.AWAITING_APPROVAL
        return RunStatus.PLANNED


def _pending_items(state: RunState) -> list[WorkItem]:
    return [
        state.work_items[item_id]
        for item_id in sorted(state.work_items)
        if (result := state.results.get(item_id)) is None
        or result.status in _RESUMABLE_STATUSES
    ]


def _developer_instructions(state: RunState) -> str:
    sections = [
        "Respect the declared sandbox and make only the bounded change described by the work item."
    ]
    sections.extend(
        f"Repository instructions from {instruction.path}:\n{instruction.content}"
        for instruction in state.repository.instruction_files
    )
    return "\n\n".join(sections)


def _failure_result(
    item: WorkItem,
    *,
    status: WorkStatus,
    message: str,
) -> WorkResult:
    return WorkResult(
        work_item_id=item.id,
        thread_id=None,
        turn_id=None,
        status=status,
        final_response=None,
        sdk_items=tuple[JsonValue](),
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=0,
        duration_seconds=0.0,
        changed_paths=(),
        verification=(),
        confidence=0.0,
        escalation_requested=False,
        error=message,
    )
