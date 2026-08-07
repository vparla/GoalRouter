# SPDX-License-Identifier: MIT
# File: tests/unit/test_config.py
# Purpose: Verify routing YAML schema, semantic validation, and parsing

from hashlib import sha256
from pathlib import Path

import pytest
import yaml

from goalrouter.config import RouterConfig, load_router_config
from goalrouter.domain import ApprovalMode, SandboxMode
from goalrouter.errors import ConfigurationError

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "config/task-models.schema.json"
FIXTURES = ROOT / "tests/fixtures/config"


def _load(path: Path) -> RouterConfig:
    return load_router_config(path, schema_path=SCHEMA)


def _valid_raw() -> dict[str, object]:
    loaded = yaml.safe_load((FIXTURES / "valid-custom-task.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _write_yaml(tmp_path: Path, raw: dict[str, object]) -> Path:
    path = tmp_path / "task-models.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


def test_loads_arbitrary_task_and_computes_digest() -> None:
    path = FIXTURES / "valid-custom-task.yaml"

    config = _load(path)

    assert config.schema_version == 1
    assert config.maximum_read_concurrency == 2
    assert config.repository_inspection_timeout_seconds == 120
    assert "database-migration" in config.tasks
    assert config.tasks["database-migration"].sandbox is SandboxMode.WORKSPACE_WRITE
    assert config.tasks["database-migration"].approval is ApprovalMode.REQUIRED
    assert config.model_aliases["frontier"].rank == 20
    assert config.digest == sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "field",
    [
        "schema-version",
        "default-task",
        "planner-task",
        "completion-task",
        "maximum-read-concurrency",
        "repository-inspection-timeout-seconds",
        "model-aliases",
        "tasks",
    ],
)
def test_rejects_missing_required_top_level_fields(tmp_path: Path, field: str) -> None:
    raw = _valid_raw()
    del raw[field]

    with pytest.raises(ConfigurationError, match=field):
        _load(_write_yaml(tmp_path, raw))


def test_rejects_duplicate_or_nonpositive_alias_ranks(tmp_path: Path) -> None:
    raw = _valid_raw()
    aliases = raw["model-aliases"]
    assert isinstance(aliases, dict)
    frontier = aliases["frontier"]
    assert isinstance(frontier, dict)
    frontier["rank"] = 10

    with pytest.raises(ConfigurationError, match="rank"):
        _load(_write_yaml(tmp_path, raw))

    frontier["rank"] = 0
    with pytest.raises(ConfigurationError, match="rank"):
        _load(_write_yaml(tmp_path, raw))


@pytest.mark.parametrize(
    ("field", "value"),
    [("reasoning-effort", "impossible"), ("sandbox", "full-access")],
)
def test_rejects_unknown_policy_enum_values(tmp_path: Path, field: str, value: str) -> None:
    raw = _valid_raw()
    if field == "reasoning-effort":
        aliases = raw["model-aliases"]
        assert isinstance(aliases, dict)
        alias = aliases["economy"]
        assert isinstance(alias, dict)
        alias[field] = value
    else:
        tasks = raw["tasks"]
        assert isinstance(tasks, dict)
        task = tasks["bounded-diagnosis"]
        assert isinstance(task, dict)
        task[field] = value

    with pytest.raises(ConfigurationError, match=field):
        _load(_write_yaml(tmp_path, raw))


@pytest.mark.parametrize("field", ["default-task", "planner-task", "completion-task"])
def test_rejects_unknown_required_task_references(tmp_path: Path, field: str) -> None:
    raw = _valid_raw()
    raw[field] = "absent-task"

    with pytest.raises(ConfigurationError, match=field):
        _load(_write_yaml(tmp_path, raw))


def test_rejects_unknown_alias_and_escalation_references() -> None:
    with pytest.raises(ConfigurationError, match="missing"):
        _load(FIXTURES / "invalid-alias.yaml")

    with pytest.raises(ConfigurationError, match="absent-task"):
        _load(FIXTURES / "invalid-escalation.yaml")


def test_rejects_unknown_hard_risk_minimum_alias(tmp_path: Path) -> None:
    raw = _valid_raw()
    rules = raw["hard-risk-rules"]
    assert isinstance(rules, list)
    rule = rules[0]
    assert isinstance(rule, dict)
    rule["minimum-model-alias"] = "absent-alias"

    with pytest.raises(ConfigurationError, match="absent-alias"):
        _load(_write_yaml(tmp_path, raw))


def test_malformed_and_unsafe_yaml_fail_with_file_context(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("tasks: [", encoding="utf-8")
    unsafe = tmp_path / "unsafe.yaml"
    unsafe.write_text("!!python/object/apply:os.system ['echo unsafe']", encoding="utf-8")

    for path in (malformed, unsafe):
        with pytest.raises(ConfigurationError, match=path.name):
            _load(path)
