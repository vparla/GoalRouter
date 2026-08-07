# SPDX-License-Identifier: MIT
# File: src/goalrouter/cli.py
# Purpose: Command-line grammar and composition root for GoalRouter

"""Docker-oriented GoalRouter command line."""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from goalrouter.application import GoalRouterApplication
from goalrouter.approvals import ApprovalService
from goalrouter.build_info import current_build_info
from goalrouter.config import RouterConfig, load_router_config
from goalrouter.domain import AuthMode, JsonValue, RouteDecision, RunState
from goalrouter.errors import ConfigurationError, GoalRouterError
from goalrouter.locking import FileRunLease, ProjectDirectoryWriteLease
from goalrouter.planner import StructuredPlanner
from goalrouter.reporting import ReportRenderer
from goalrouter.repository import LocalRepositoryInspector
from goalrouter.routing import TaskRouter
from goalrouter.scheduler import WorkScheduler
from goalrouter.sdk.codex import CodexSdkClient
from goalrouter.storage.json_store import JsonRunStore


def build_parser() -> argparse.ArgumentParser:
    """Build the complete, side-effect-free CLI parser."""

    parser = argparse.ArgumentParser(
        prog="goalrouter",
        description="Route local engineering work through repository-neutral Codex policy.",
    )
    parser.add_argument("--config", type=Path, help="routing YAML path")
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument(
        "--auth-mode",
        choices=tuple(mode.value for mode in AuthMode),
        help="override GOALROUTER_AUTH_MODE",
    )
    parser.add_argument("--state-path", type=Path, help="override GOALROUTER_STATE_PATH")
    parser.add_argument("--codex-bin", help="explicit advanced Codex binary path")
    commands = parser.add_subparsers(dest="command", required=True)

    config = commands.add_parser("config", help="configuration operations")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("template", help="emit the shipped configuration template")
    config_commands.add_parser("validate", help="validate configuration and references")

    commands.add_parser("version", help="show package, protocol, and image metadata")
    commands.add_parser("models", help="list and validate available account models")

    route = commands.add_parser("route", help="resolve one explicit task route")
    _project_argument(route)
    route.add_argument("--task", required=True)
    route.add_argument("--prompt", required=True)
    _affected_paths(route)

    plan = commands.add_parser("plan", help="discover and persist an objective plan")
    _project_argument(plan)
    plan.add_argument("--objective", required=True)
    plan.add_argument("--run-id")

    run = commands.add_parser("run", help="run one task or a compound objective")
    _project_argument(run)
    mode = run.add_mutually_exclusive_group(required=True)
    mode.add_argument("--task")
    mode.add_argument("--objective")
    run.add_argument("--prompt")
    run.add_argument("--run-id")
    _affected_paths(run)

    status = commands.add_parser("status", help="show persisted run status")
    status.add_argument("run_id")

    approve = commands.add_parser("approve", help="approve one exact work-item fingerprint")
    approve.add_argument("run_id")
    approve.add_argument("work_item_id")
    approve.add_argument("--approved-by", required=True)

    resume = commands.add_parser("resume", help="continue unfinished persisted work")
    resume.add_argument("run_id")
    resume.add_argument("--acknowledge-configuration-change", action="store_true")

    report = commands.add_parser("report", help="render and persist a run report")
    report.add_argument("run_id")
    return parser


async def async_main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Parse, compose, dispatch, and map expected failures to stable exit codes."""

    args = build_parser().parse_args(argv)
    active_environ = os.environ if environ is None else environ
    try:
        _validate_arguments(args)
        if args.command == "version":
            _emit(current_build_info(active_environ).to_json_value(), json_output=args.json_output)
            return 0
        if args.command == "config" and args.config_command == "template":
            template_bytes = await asyncio.to_thread(_shipped_template)
            if args.json_output:
                _emit({"template": template_bytes.decode("utf-8")}, json_output=True)
            else:
                sys.stdout.buffer.write(template_bytes)
            return 0
        config_path = args.config or Path(
            active_environ.get("GOALROUTER_CONFIG", "/etc/goalrouter/task-models.yaml")
        )
        schema_value = active_environ.get("GOALROUTER_SCHEMA")
        config = load_router_config(
            config_path,
            schema_path=Path(schema_value) if schema_value else None,
        )
        if args.command == "config":
            _emit(_config_summary(config, config_path), json_output=args.json_output)
            return 0

        application = _compose_application(
            config,
            config_path,
            active_environ,
            auth_mode_override=args.auth_mode,
            state_path_override=args.state_path,
            codex_bin_override=args.codex_bin,
        )
        output = await _dispatch(args, application)
        _emit(output, json_output=args.json_output)
        return 0
    except GoalRouterError as error:
        print(f"goalrouter: {error}", file=sys.stderr)
        return error.exit_code


def main() -> int:
    """Synchronous console-script boundary."""

    return asyncio.run(async_main())


async def _dispatch(
    args: argparse.Namespace,
    application: GoalRouterApplication,
) -> JsonValue | RouteDecision | RunState | str:
    if args.command == "models":
        return {"models": list[JsonValue](sorted(await application.models()))}
    if args.command == "route":
        return await application.route_task(
            project_path=args.project,
            task=args.task,
            prompt=args.prompt,
            affected_paths=args.affected_paths,
        )
    if args.command == "plan":
        return await application.plan_objective(
            project_path=args.project,
            prompt=args.objective,
            run_id=args.run_id,
        )
    if args.command == "run" and args.task is not None:
        return await application.run_task(
            project_path=args.project,
            task=args.task,
            prompt=args.prompt,
            affected_paths=args.affected_paths,
            run_id=args.run_id,
        )
    if args.command == "run":
        return await application.run_objective(
            project_path=args.project,
            prompt=args.objective,
            run_id=args.run_id,
        )
    if args.command == "status":
        return await application.status(args.run_id)
    if args.command == "approve":
        return await application.approve(
            args.run_id,
            args.work_item_id,
            approved_by=args.approved_by,
        )
    if args.command == "resume":
        return await application.resume(
            args.run_id,
            acknowledge_configuration_change=args.acknowledge_configuration_change,
        )
    if args.command == "report":
        return await application.report(args.run_id)
    raise ConfigurationError(f"Unsupported command {args.command!r}")


def _compose_application(
    config: RouterConfig,
    config_path: Path,
    environ: Mapping[str, str],
    *,
    auth_mode_override: str | None,
    state_path_override: Path | None,
    codex_bin_override: str | None,
) -> GoalRouterApplication:
    try:
        auth_mode = AuthMode(
            auth_mode_override
            or environ.get("GOALROUTER_AUTH_MODE", AuthMode.EXISTING_SESSION.value)
        )
    except ValueError as error:
        raise ConfigurationError("GOALROUTER_AUTH_MODE is invalid") from error
    state_path = state_path_override or Path(
        environ.get("GOALROUTER_STATE_PATH", ".goalrouter/runs")
    )
    codex_bin = codex_bin_override or environ.get("GOALROUTER_CODEX_BIN")
    client = CodexSdkClient(
        auth_mode,
        api_key=environ.get("OPENAI_API_KEY"),
        environ=environ,
        codex_bin=codex_bin,
    )
    router = TaskRouter(config)
    store = JsonRunStore(state_path)
    approvals = ApprovalService()
    planner_schema = environ.get("GOALROUTER_PLANNER_SCHEMA")
    planner = StructuredPlanner(
        client,
        router,
        schema_path=Path(planner_schema) if planner_schema else None,
    )
    scheduler = WorkScheduler(
        client,
        store,
        approvals,
        project_write_lease=ProjectDirectoryWriteLease(),
        max_read_concurrency=config.maximum_read_concurrency,
    )
    return GoalRouterApplication(
        config=config,
        config_path=config_path,
        client=client,
        repository=LocalRepositoryInspector(
            timeout_seconds=config.repository_inspection_timeout_seconds
        ),
        planner=planner,
        router=router,
        scheduler=scheduler,
        approvals=approvals,
        store=store,
        reporter=ReportRenderer(),
        run_lease=FileRunLease(state_path),
    )


def _validate_arguments(args: argparse.Namespace) -> None:
    if args.command == "run" and args.task is not None and not args.prompt:
        raise ConfigurationError("Task mode requires --prompt")
    if args.command == "run" and args.objective is not None and args.prompt is not None:
        raise ConfigurationError("Objective mode does not accept --prompt")


def _emit(
    output: JsonValue | RouteDecision | RunState | str,
    *,
    json_output: bool,
) -> None:
    normalized = _json_output(output)
    if json_output:
        print(json.dumps(normalized, indent=2, sort_keys=True))
        return
    if isinstance(output, str):
        print(output, end="" if output.endswith("\n") else "\n")
    elif isinstance(output, RouteDecision):
        print(
            f"Task {output.task}: {output.model} ({output.reasoning_effort}, "
            f"{output.sandbox.value}, approval {output.approval.value})\n"
            f"Reason: {output.reason}"
        )
    elif isinstance(output, RunState):
        print(
            f"Run {output.run_id}: {output.status.value} "
            f"({len(output.results)}/{len(output.work_items)} work items terminal)"
        )
    elif (
        isinstance(normalized, dict)
        and set(normalized) == {"models"}
        and isinstance(models := normalized.get("models"), list)
    ):
        for model in models:
            print(model)
    else:
        print(json.dumps(normalized, indent=2, sort_keys=True))


def _json_output(output: JsonValue | RouteDecision | RunState | str) -> JsonValue:
    if isinstance(output, RouteDecision | RunState):
        return output.to_dict()
    if isinstance(output, str):
        return {"report": output}
    return output


def _config_summary(config: RouterConfig, path: Path) -> dict[str, JsonValue]:
    return {
        "status": "valid",
        "path": str(path),
        "schema_version": config.schema_version,
        "configuration_digest": config.digest,
        "tasks": list[JsonValue](sorted(config.tasks)),
        "models": list[JsonValue](
            sorted({alias.model for alias in config.model_aliases.values()})
        ),
    }


def _shipped_template() -> bytes:
    return Path("/etc/goalrouter/task-models.template.yaml").read_bytes()


def _project_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", required=True, type=Path)


def _affected_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--affected-path",
        action="append",
        dest="affected_paths",
        default=[],
        type=Path,
    )
