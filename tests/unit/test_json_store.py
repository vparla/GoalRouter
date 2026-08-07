# SPDX-License-Identifier: MIT
# File: tests/unit/test_json_store.py
# Purpose: Verify atomic resumable JSON state and append-only event persistence

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from goalrouter.domain import (
    Objective,
    RepositoryContext,
    RunEvent,
    RunState,
    RunStatus,
)
from goalrouter.errors import ResumeConfigurationChangedError, StateError
from goalrouter.reporting import ReportRenderer
from goalrouter.storage.json_store import JsonRunStore, redact_json
from goalrouter.storage.protocol import RunStoreProtocol


def _state(
    *,
    digest: str = "digest",
    status: RunStatus = RunStatus.PLANNED,
    branch: str | None = None,
    dirty_paths: tuple[Path, ...] = (),
) -> RunState:
    project = Path("/projects/example")
    return RunState(
        schema_version=1,
        configuration_digest=digest,
        objective=Objective(
            id="run-1",
            prompt="Inspect the project",
            project_path=project,
            explicit_task=None,
            config_path=Path("/etc/goalrouter/task-models.yaml"),
            created_at=datetime(2026, 8, 3, tzinfo=UTC),
        ),
        repository=RepositoryContext(
            project_path=project,
            is_git_worktree=branch is not None,
            branch=branch,
            dirty_paths=dirty_paths,
            instruction_files=(),
            language_counts=(),
            docker_files=(),
            command_errors=(),
        ),
        work_items={},
        routes={},
        results={},
        approvals={},
        status=status,
    )


def _accepts_protocol(store: RunStoreProtocol) -> RunStoreProtocol:
    return store


@pytest.mark.asyncio
async def test_create_uses_exact_layout_and_round_trips(tmp_path: Path) -> None:
    store = JsonRunStore(tmp_path / "runs")
    _accepts_protocol(store)
    state = _state()

    await store.create(state)

    run_dir = tmp_path / "runs/run-1"
    assert sorted(path.name for path in run_dir.iterdir()) == ["events.jsonl", "state.json"]
    assert await store.load("run-1") == state


@pytest.mark.asyncio
async def test_atomic_save_leaves_no_temporary_file(tmp_path: Path) -> None:
    store = JsonRunStore(tmp_path / "runs")
    await store.create(_state())

    await store.save(_state(status=RunStatus.RUNNING))

    run_dir = tmp_path / "runs/run-1"
    assert not tuple(run_dir.glob("*.tmp"))
    assert (await store.load("run-1")).status is RunStatus.RUNNING


@pytest.mark.asyncio
async def test_concurrent_saves_for_one_run_are_serialized(tmp_path: Path) -> None:
    store = JsonRunStore(tmp_path / "runs")
    await store.create(_state())

    async with asyncio.TaskGroup() as group:
        group.create_task(store.save(_state(status=RunStatus.RUNNING)))
        group.create_task(store.save(_state(status=RunStatus.COMPLETED)))

    loaded = await store.load("run-1")
    assert loaded.status in {RunStatus.RUNNING, RunStatus.COMPLETED}
    json.loads((tmp_path / "runs/run-1/state.json").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_events_are_append_only_and_reports_use_stable_paths(tmp_path: Path) -> None:
    store = JsonRunStore(tmp_path / "runs")
    await store.create(_state())
    first = RunEvent(
        timestamp=datetime(2026, 8, 3, 10, 0, tzinfo=UTC),
        event="planned",
        work_item_id=None,
        details={"count": 2},
    )
    second = RunEvent(
        timestamp=datetime(2026, 8, 3, 10, 1, tzinfo=UTC),
        event="started",
        work_item_id="work-1",
        details={},
    )

    await store.append_event("run-1", first)
    await store.append_event("run-1", second)
    report_path = await store.write_report("run-1", "# Report\n")

    lines = (tmp_path / "runs/run-1/events.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["event"] for line in lines] == ["planned", "started"]
    assert report_path == tmp_path / "runs/run-1/report.md"
    assert report_path.read_text(encoding="utf-8") == "# Report\n"


@pytest.mark.asyncio
async def test_surrogateescaped_dirty_path_round_trips_through_state_and_report(
    tmp_path: Path,
) -> None:
    branch = os.fsdecode(b"topic-\xff")
    dirty_path = Path(os.fsdecode(b"invalid-\xff"))
    dirty_paths = (
        dirty_path,
        Path("line\nfeed"),
        Path("tab\tname"),
        Path("control-\x01"),
        Path(r"back\slash"),
    )
    store = JsonRunStore(tmp_path / "runs")
    await store.create(_state(branch=branch, dirty_paths=dirty_paths))

    loaded = await store.load("run-1")
    report = ReportRenderer().render(loaded)
    report_path = await store.write_report("run-1", report)

    assert os.fsencode(loaded.repository.dirty_paths[0]) == b"invalid-\xff"
    assert "topic-\\xff" in report
    assert "invalid-\\xff" in report
    assert "line\\nfeed" in report
    assert "tab\\tname" in report
    assert "control-\\x01" in report
    assert r"back\\slash" in report
    assert "line\nfeed" not in report
    assert report_path.read_text(encoding="utf-8") == report


def test_redact_json_removes_secret_keys_and_api_key_shaped_values() -> None:
    redacted = redact_json(
        {
            "OPENAI_API_KEY": "sk-secret-value",
            "nested": [{"authorization": "Bearer value"}, "sk-another-secret"],
            "input_tokens": 42,
        }
    )

    assert redacted == {
        "OPENAI_API_KEY": "[REDACTED]",
        "nested": [{"authorization": "[REDACTED]"}, "[REDACTED]"],
        "input_tokens": 42,
    }


@pytest.mark.asyncio
async def test_corrupt_and_unsupported_state_fail_closed(tmp_path: Path) -> None:
    store = JsonRunStore(tmp_path / "runs")
    await store.create(_state())
    state_path = tmp_path / "runs/run-1/state.json"

    state_path.write_text("not-json", encoding="utf-8")
    with pytest.raises(StateError, match="corrupt"):
        await store.load("run-1")

    unsupported = _state().to_dict()
    unsupported["schema_version"] = 999
    state_path.write_text(json.dumps(unsupported), encoding="utf-8")
    with pytest.raises(StateError, match="schema version"):
        await store.load("run-1")


@pytest.mark.asyncio
async def test_resume_digest_change_requires_explicit_acknowledgement(tmp_path: Path) -> None:
    store = JsonRunStore(tmp_path / "runs")
    await store.create(_state(digest="old"))

    with pytest.raises(ResumeConfigurationChangedError):
        await store.load("run-1", expected_configuration_digest="new")

    loaded = await store.load(
        "run-1",
        expected_configuration_digest="new",
        acknowledge_configuration_change=True,
    )
    assert loaded.configuration_digest == "old"


@pytest.mark.asyncio
async def test_invalid_run_id_is_rejected_before_state_path_access(tmp_path: Path) -> None:
    store = JsonRunStore(tmp_path / "runs")

    with pytest.raises(StateError, match="Invalid run ID"):
        await store.load("../escape")

    assert not (tmp_path / "escape").exists()
