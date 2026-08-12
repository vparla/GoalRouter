# SPDX-License-Identifier: MIT
# File: tests/integration/test_cli_end_to_end.py
# Purpose: Verify command dispatch and JSON/human output through the CLI boundary

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Never

import pytest
import yaml

import goalrouter.cli as cli
from goalrouter.domain import (
    ApprovalMode,
    Objective,
    RepositoryContext,
    RouteDecision,
    RouteSource,
    RunState,
    RunStatus,
    SandboxMode,
)
from goalrouter.errors import ProjectBusyError, RunBusyError

ROOT = Path(__file__).resolve().parents[2]


def _route() -> RouteDecision:
    return RouteDecision(
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
        source=RouteSource.EXPLICIT,
        reason="explicit task",
    )


def _state() -> RunState:
    return RunState(
        schema_version=1,
        configuration_digest="digest",
        objective=Objective(
            id="run-1",
            prompt="prompt",
            project_path=Path("/project"),
            explicit_task="repository-search",
            config_path=Path("/config.yaml"),
            created_at=datetime(2026, 8, 3, tzinfo=UTC),
        ),
        repository=RepositoryContext(
            project_path=Path("/project"),
            is_git_worktree=False,
            branch=None,
            dirty_paths=(),
            instruction_files=(),
            language_counts=(),
            docker_files=(),
            command_errors=(),
        ),
        work_items={},
        routes={},
        results={},
        approvals={},
        status=RunStatus.COMPLETED,
    )


class FakeApplication:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def models(self) -> frozenset[str]:
        self.calls.append(("models", None))
        return frozenset({"model-b", "model-a"})

    async def route_task(self, **kwargs: object) -> RouteDecision:
        self.calls.append(("route", kwargs))
        return _route()

    async def plan_objective(self, **kwargs: object) -> RunState:
        self.calls.append(("plan", kwargs))
        return _state()

    async def run_task(self, **kwargs: object) -> RunState:
        self.calls.append(("run-task", kwargs))
        return _state()

    async def run_objective(self, **kwargs: object) -> RunState:
        self.calls.append(("run-objective", kwargs))
        return _state()

    async def status(self, run_id: str) -> RunState:
        self.calls.append(("status", run_id))
        return _state()

    async def approve(self, run_id: str, work_item_id: str, **kwargs: object) -> RunState:
        self.calls.append(("approve", (run_id, work_item_id, kwargs)))
        return _state()

    async def resume(self, run_id: str, **kwargs: object) -> RunState:
        self.calls.append(("resume", (run_id, kwargs)))
        return _state()

    async def report(self, run_id: str) -> str:
        self.calls.append(("report", run_id))
        return "report-body\n"


@pytest.mark.asyncio
async def test_version_and_template_dispatch_without_application_composition(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unexpected_composition(*args: object, **kwargs: object) -> Never:
        del args, kwargs
        raise AssertionError("metadata commands must not compose the application")

    monkeypatch.setattr(cli, "_compose_application", unexpected_composition)

    assert await cli.async_main(["--json", "version"], environ={}) == 0
    version_output = json.loads(capsys.readouterr().out)
    assert version_output == {
        "version": "1.0.2",
        "protocol_version": 1,
        "image_reference": None,
        "image_revision": None,
    }

    assert await cli.async_main(["config", "template"], environ={}) == 0
    template_output = capsys.readouterr().out
    expected_template = (
        await asyncio.to_thread(Path("/etc/goalrouter/task-models.template.yaml").read_bytes)
    ).decode("utf-8")
    assert template_output == expected_template
    assert yaml.safe_load(template_output) is not None

    assert await cli.async_main(["--json", "config", "template"], environ={}) == 0
    assert json.loads(capsys.readouterr().out) == {"template": expected_template}


@pytest.mark.asyncio
async def test_json_route_and_objective_run_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    application = FakeApplication()
    monkeypatch.setattr(cli, "_compose_application", lambda *args, **kwargs: application)
    config = str(ROOT / "config/task-models.yaml")
    environ = {"GOALROUTER_SCHEMA": str(ROOT / "config/task-models.schema.json")}

    route_code = await cli.async_main(
        [
            "--config",
            config,
            "--json",
            "route",
            "--project",
            "/project",
            "--task",
            "repository-search",
            "--prompt",
            "find it",
        ],
        environ=environ,
    )
    route_output = json.loads(capsys.readouterr().out)
    run_code = await cli.async_main(
        [
            "--config",
            config,
            "--json",
            "run",
            "--project",
            "/project",
            "--objective",
            "complete it",
        ],
        environ=environ,
    )
    run_output = json.loads(capsys.readouterr().out)

    assert (route_code, run_code) == (0, 0)
    assert route_output["task"] == "repository-search"
    assert run_output["status"] == "completed"
    assert [call[0] for call in application.calls] == ["route", "run-objective"]


@pytest.mark.asyncio
async def test_models_approve_resume_status_and_report_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    application = FakeApplication()
    monkeypatch.setattr(cli, "_compose_application", lambda *args, **kwargs: application)
    config_args = ["--config", str(ROOT / "config/task-models.yaml")]
    environ = {"GOALROUTER_SCHEMA": str(ROOT / "config/task-models.schema.json")}

    commands = [
        [*config_args, "models"],
        [*config_args, "status", "run-1"],
        [
            *config_args,
            "approve",
            "run-1",
            "work-1",
            "--approved-by",
            "operator",
        ],
        [*config_args, "resume", "run-1", "--acknowledge-configuration-change"],
        [*config_args, "report", "run-1"],
    ]
    for arguments in commands:
        assert await cli.async_main(arguments, environ=environ) == 0
    output = capsys.readouterr().out

    assert "model-a" in output
    assert "run-1" in output
    assert "report-body" in output
    assert [call[0] for call in application.calls] == [
        "models",
        "status",
        "approve",
        "resume",
        "report",
    ]


def test_composition_injects_concrete_run_and_project_leases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_lease = object()
    project_lease = object()
    scheduler = object()
    run_paths: list[Path] = []
    scheduler_arguments: list[dict[str, object]] = []
    application_arguments: list[dict[str, object]] = []
    repository_timeouts: list[float] = []

    def make_run_lease(path: Path) -> object:
        run_paths.append(path)
        return run_lease

    def make_scheduler(*args: object, **kwargs: object) -> object:
        del args
        scheduler_arguments.append(kwargs)
        return scheduler

    def make_application(**kwargs: object) -> object:
        application_arguments.append(kwargs)
        return object()

    def make_repository(*, timeout_seconds: float) -> object:
        repository_timeouts.append(timeout_seconds)
        return object()

    monkeypatch.setattr(cli, "FileRunLease", make_run_lease, raising=False)
    monkeypatch.setattr(
        cli,
        "ProjectDirectoryWriteLease",
        lambda: project_lease,
        raising=False,
    )
    monkeypatch.setattr(cli, "WorkScheduler", make_scheduler)
    monkeypatch.setattr(cli, "LocalRepositoryInspector", make_repository)
    monkeypatch.setattr(cli, "GoalRouterApplication", make_application)
    config = cli.load_router_config(
        ROOT / "config/task-models.yaml",
        schema_path=ROOT / "config/task-models.schema.json",
    )

    cli._compose_application(
        config,
        ROOT / "config/task-models.yaml",
        {},
        auth_mode_override=None,
        state_path_override=Path("/selected-state"),
        codex_bin_override=None,
    )

    assert run_paths == [Path("/selected-state")]
    assert scheduler_arguments[0]["project_write_lease"] is project_lease
    assert application_arguments[0]["scheduler"] is scheduler
    assert application_arguments[0]["run_lease"] is run_lease
    assert repository_timeouts == [120]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_exit"),
    (
        (ProjectBusyError("Project is busy"), 14),
        (RunBusyError("Run is busy"), 15),
    ),
)
async def test_cli_maps_busy_lease_errors_to_stable_exits(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: ProjectBusyError | RunBusyError,
    expected_exit: int,
) -> None:
    class BusyApplication(FakeApplication):
        async def run_task(self, **kwargs: object) -> RunState:
            self.calls.append(("run-task", kwargs))
            raise error

    application = BusyApplication()
    monkeypatch.setattr(cli, "_compose_application", lambda *args, **kwargs: application)

    exit_code = await cli.async_main(
        [
            "--config",
            str(ROOT / "config/task-models.yaml"),
            "run",
            "--project",
            "/project",
            "--task",
            "repository-search",
            "--prompt",
            "do not dispatch",
        ],
        environ={"GOALROUTER_SCHEMA": str(ROOT / "config/task-models.schema.json")},
    )

    captured = capsys.readouterr()
    assert exit_code == expected_exit
    assert captured.out == ""
    assert str(error) in captured.err
    assert application.calls == [
        (
            "run-task",
            {
                "project_path": Path("/project"),
                "task": "repository-search",
                "prompt": "do not dispatch",
                "affected_paths": [],
                "run_id": None,
            },
        )
    ]
