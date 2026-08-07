# SPDX-License-Identifier: MIT
# File: tests/integration/test_validation_routes.py
# Purpose: Validate one generic routing policy against neutral mounted projects

import os
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from goalrouter.application import GoalRouterApplication
from goalrouter.approvals import ApprovalService
from goalrouter.config import load_router_config
from goalrouter.domain import (
    AccessMode,
    Objective,
    RepositoryContext,
    RouteDecision,
    SandboxMode,
    WorkItem,
    WorkResult,
)
from goalrouter.errors import RepositoryError
from goalrouter.reporting import ReportRenderer
from goalrouter.repository import CommandResult, LocalRepositoryInspector
from goalrouter.routing import TaskRouter
from goalrouter.scheduler import WorkScheduler
from goalrouter.storage.json_store import JsonRunStore

ROOT = Path(__file__).resolve().parents[2]
PROJECT_VARIABLES = (
    "VALIDATION_PROJECT_ONE",
    "VALIDATION_PROJECT_TWO",
    "VALIDATION_PROJECT_THREE",
)
validation_profile = pytest.mark.skipif(
    not any(os.environ.get(variable) for variable in PROJECT_VARIABLES),
    reason="run with the Compose validation profile",
)
COMMON_TASKS = (
    "repository-search",
    "docker-invoke",
    "unit-test-run",
    "unit-test-debug",
    "architecture-change",
    "security-sensitive",
)


class NoOpRunLease:
    @asynccontextmanager
    async def acquire(self, run_id: str) -> AsyncIterator[None]:
        del run_id
        yield


class NoOpProjectWriteLease:
    @asynccontextmanager
    async def acquire(self, project_path: Path) -> AsyncIterator[None]:
        del project_path
        yield


def _project_path(variable: str) -> Path:
    value = os.environ.get(variable)
    assert value, f"validation profile must define {variable}"
    assert os.environ.get(f"{variable}_MOUNTED") == "1", (
        f"validation profile host mount is missing for {variable}"
    )
    return Path(value)


class StaticCommandRunner:
    def __init__(self, root: Path) -> None:
        self._root = root
        self.calls: list[tuple[str, ...]] = []

    async def run_read_only(
        self, argv: Sequence[str], *, cwd: Path
    ) -> CommandResult:
        del cwd
        call = tuple(argv)
        self.calls.append(call)
        if call[-1] == "--show-toplevel":
            return CommandResult(call, 0, f"{self._root}\n", "")
        if call[-1] == "--show-current":
            return CommandResult(call, 0, "security-test\n", "")
        if "--porcelain=v1" in call:
            return CommandResult(call, 0, "", "")
        raise AssertionError(f"Unexpected command: {call}")


class FailingGitPhaseRunner:
    def __init__(self, root: Path, failed_command_token: str) -> None:
        self._root = root
        self._failed_command_token = failed_command_token

    async def run_read_only(
        self, argv: Sequence[str], *, cwd: Path
    ) -> CommandResult:
        del cwd
        call = tuple(argv)
        if "--absolute-git-dir" in call:
            return CommandResult(call, 0, f"{self._root}\n{self._root / '.git'}\n", "")
        if call[-1] == "--show-current":
            return self._result(call, "security-test\n")
        if "--stage" in call:
            return self._result(call)
        if "ls-tree" in call:
            return self._result(call)
        if "--others" in call:
            return self._result(call)
        raise AssertionError(f"Unexpected command: {call}")

    def _result(self, call: tuple[str, ...], stdout: str = "") -> CommandResult:
        if self._failed_command_token in call:
            return CommandResult(call, 129, "", "DUMMY-REPOSITORY-DETAIL")
        return CommandResult(call, 0, stdout, "")


class RecordingPlanner:
    def __init__(self) -> None:
        self.calls = 0

    async def plan(
        self,
        objective: Objective,
        repository: RepositoryContext,
        config: object,
    ) -> tuple[WorkItem, ...]:
        del objective, repository, config
        self.calls += 1
        return ()


class RecordingClient:
    def __init__(self, models: frozenset[str]) -> None:
        self._models = models
        self.inventory_calls = 0
        self.turn_calls = 0

    async def available_models(self) -> frozenset[str]:
        self.inventory_calls += 1
        return self._models

    async def run_new_thread(
        self,
        *,
        project_path: Path,
        route: RouteDecision,
        prompt: str,
        developer_instructions: str,
        output_schema: Mapping[str, object] | None = None,
    ) -> WorkResult:
        del project_path, route, prompt, developer_instructions, output_schema
        self.turn_calls += 1
        raise AssertionError("rejected instruction content reached an SDK turn")

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
        self.turn_calls += 1
        raise AssertionError("rejected instruction content reached an SDK turn")


@pytest.mark.parametrize("variable", PROJECT_VARIABLES)
@pytest.mark.asyncio
@validation_profile
async def test_generic_routes_resolve_from_dynamic_repository_evidence(
    variable: str,
) -> None:
    project = _project_path(variable)
    context = await LocalRepositoryInspector(timeout_seconds=120).inspect(project)
    config = load_router_config(
        ROOT / "config/task-models.yaml",
        schema_path=ROOT / "config/task-models.schema.json",
    )
    router = TaskRouter(config)
    language_task = _language_task(context.language_counts)
    aliases: set[str] = set()

    assert context.instruction_files
    for task in (*COMMON_TASKS, language_task):
        provisional = WorkItem(
            id=task,
            title=task,
            instructions=f"Validate generic route {task}",
            task=task,
            phase="validation",
            dependencies=(),
            access=AccessMode.READ_ONLY,
            affected_paths=(),
            expected_result="A deterministic route decision.",
            verification=(),
            confidence=1.0,
            risk_flags=frozenset(),
        )
        route = router.route(provisional, explicit_task=task)
        aliases.add(route.model_alias)
        assert route.model == config.model_aliases[route.model_alias].model
        assert route.sandbox in {SandboxMode.READ_ONLY, SandboxMode.WORKSPACE_WRITE}
        assert route.reason.startswith("explicit task")

    assert aliases >= {"economy", "balanced", "frontier", "assurance"}
    assert len(config.digest) == 64


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("plan", "task"))
async def test_unsafe_instruction_fails_before_planner_or_sdk_turn(
    tmp_path: Path,
    operation: str,
) -> None:
    project = tmp_path / "project"
    auth = tmp_path / "codex-auth"
    project.mkdir()
    auth.mkdir()
    marker = "DUMMY-INTEGRATION-NON-SECRET-MARKER"
    credential = auth / "auth.json"
    credential.write_text(marker, encoding="utf-8")
    (project / "AGENTS.md").symlink_to(credential)
    config = load_router_config(
        ROOT / "config/task-models.yaml",
        schema_path=ROOT / "config/task-models.schema.json",
    )
    models = frozenset(alias.model for alias in config.model_aliases.values())
    client = RecordingClient(models)
    planner = RecordingPlanner()
    store = JsonRunStore(tmp_path / "state")
    router = TaskRouter(config)
    command_runner = StaticCommandRunner(project)
    application = GoalRouterApplication(
        config=config,
        config_path=ROOT / "config/task-models.yaml",
        client=client,
        repository=LocalRepositoryInspector(command_runner, timeout_seconds=10),
        planner=planner,
        router=router,
        scheduler=WorkScheduler(
            client,
            store,
            ApprovalService(),
            project_write_lease=NoOpProjectWriteLease(),
            max_read_concurrency=config.maximum_read_concurrency,
        ),
        approvals=ApprovalService(),
        store=store,
        reporter=ReportRenderer(),
        run_lease=NoOpRunLease(),
    )

    with pytest.raises(
        RepositoryError, match=r"(?i)unsafe repository instruction"
    ) as raised:
        if operation == "plan":
            await application.plan_objective(
                project_path=project,
                prompt="Inspect safely",
                run_id="unsafe-instruction",
            )
        else:
            await application.run_task(
                project_path=project,
                task="repository-search",
                prompt="Inspect safely",
                run_id="unsafe-instruction",
            )

    assert marker not in str(raised.value)
    assert client.inventory_calls == 0
    assert client.turn_calls == 0
    assert planner.calls == 0
    assert command_runner.calls == []
    assert not (tmp_path / "state").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failed_phase", "command_token"),
    (
        ("branch", "branch"),
        ("index", "--stage"),
        ("HEAD tree", "ls-tree"),
        ("untracked", "--others"),
    ),
)
async def test_git_phase_failure_fails_before_planner_or_sdk_turn(
    tmp_path: Path,
    failed_phase: str,
    command_token: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    config = load_router_config(
        ROOT / "config/task-models.yaml",
        schema_path=ROOT / "config/task-models.schema.json",
    )
    models = frozenset(alias.model for alias in config.model_aliases.values())
    client = RecordingClient(models)
    planner = RecordingPlanner()
    store = JsonRunStore(tmp_path / "state")
    application = GoalRouterApplication(
        config=config,
        config_path=ROOT / "config/task-models.yaml",
        client=client,
        repository=LocalRepositoryInspector(
            FailingGitPhaseRunner(project, command_token), timeout_seconds=10
        ),
        planner=planner,
        router=TaskRouter(config),
        scheduler=WorkScheduler(
            client,
            store,
            ApprovalService(),
            project_write_lease=NoOpProjectWriteLease(),
            max_read_concurrency=config.maximum_read_concurrency,
        ),
        approvals=ApprovalService(),
        store=store,
        reporter=ReportRenderer(),
        run_lease=NoOpRunLease(),
    )

    with pytest.raises(
        RepositoryError, match=rf"(?i)Git {failed_phase} inspection failed"
    ) as raised:
        await application.plan_objective(
            project_path=project,
            prompt="Inspect safely",
            run_id=f"failed-git-{failed_phase}",
        )

    assert "DUMMY-REPOSITORY-DETAIL" not in str(raised.value)
    assert not isinstance(raised.value, ExceptionGroup)
    assert client.inventory_calls == 0
    assert client.turn_calls == 0
    assert planner.calls == 0
    assert not (tmp_path / "state").exists()


def _language_task(counts: tuple[tuple[str, int], ...]) -> str:
    applicable = {
        "python": "python-coding",
        "rust": "rust-coding",
        "c++": "c-cpp-coding",
        "c": "c-cpp-coding",
    }
    ranked = sorted(
        (
            (count, language, applicable[language])
            for language, count in counts
            if language in applicable
        ),
        reverse=True,
    )
    assert ranked, "validation project has no supported language evidence"
    return ranked[0][2]
