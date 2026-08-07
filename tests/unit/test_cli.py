# SPDX-License-Identifier: MIT
# File: tests/unit/test_cli.py
# Purpose: Verify CLI grammar, validation, and stable expected-error handling

from pathlib import Path

import pytest

from goalrouter.cli import async_main, build_parser


def test_parser_lists_every_command_and_global_options() -> None:
    parser = build_parser()

    help_text = parser.format_help()

    for command in (
        "config",
        "models",
        "route",
        "plan",
        "run",
        "status",
        "approve",
        "resume",
        "report",
    ):
        assert command in help_text
    assert "--config" in help_text
    assert "--json" in help_text


def test_parser_exposes_version_and_config_template() -> None:
    parser = build_parser()

    assert parser.parse_args(["version"]).command == "version"
    args = parser.parse_args(["config", "template"])
    assert (args.command, args.config_command) == ("config", "template")


def test_run_requires_exactly_one_mode_and_approval_requires_identity() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--project", "/project"])
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "run",
                "--project",
                "/project",
                "--task",
                "test",
                "--objective",
                "objective",
            ]
        )
    with pytest.raises(SystemExit):
        parser.parse_args(["approve", "run-1", "work-1"])

    parsed = parser.parse_args(
        [
            "approve",
            "run-1",
            "work-1",
            "--approved-by",
            "operator@example.com",
        ]
    )
    assert parsed.approved_by == "operator@example.com"


@pytest.mark.asyncio
async def test_expected_configuration_error_has_stable_code_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("not: [valid", encoding="utf-8")

    exit_code = await async_main(
        ["--config", str(invalid), "config", "validate"],
        environ={},
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "goalrouter:" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.asyncio
async def test_task_run_without_prompt_fails_before_runtime_composition(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = await async_main(
        [
            "--config",
            "config/task-models.yaml",
            "run",
            "--project",
            "/project",
            "--task",
            "repository-search",
        ],
        environ={"GOALROUTER_SCHEMA": "config/task-models.schema.json"},
    )

    assert exit_code == 2
    assert "--prompt" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_human_config_validation_reports_valid_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = await async_main(
        [
            "--config",
            "config/task-models.yaml",
            "config",
            "validate",
        ],
        environ={"GOALROUTER_SCHEMA": "config/task-models.schema.json"},
    )

    assert exit_code == 0
    assert '"status": "valid"' in capsys.readouterr().out
