# SPDX-License-Identifier: MIT
# File: tests/unit/test_routing.py
# Purpose: Verify deterministic task matching and route policy enforcement

from pathlib import Path
from types import MappingProxyType

import pytest

from goalrouter.config import RouterConfig, load_router_config
from goalrouter.domain import (
    AccessMode,
    ApprovalMode,
    RouteSource,
    SandboxMode,
    WorkItem,
)
from goalrouter.errors import ConfigurationError, ModelUnavailableError, UnknownTaskError
from goalrouter.routing import TaskRouter

ROOT = Path(__file__).resolve().parents[2]


def _config() -> RouterConfig:
    return load_router_config(
        ROOT / "config/task-models.yaml",
        schema_path=ROOT / "config/task-models.schema.json",
    )


def _item(
    *,
    task: str = "bounded-diagnosis",
    instructions: str = "Inspect the failure.",
    paths: tuple[Path, ...] = (),
    risks: frozenset[str] = frozenset(),
) -> WorkItem:
    return WorkItem(
        id="work-1",
        title="Bounded work",
        instructions=instructions,
        task=task,
        phase="implementation",
        dependencies=(),
        access=AccessMode.READ_ONLY,
        affected_paths=paths,
        expected_result="Evidence is recorded.",
        verification=(),
        confidence=0.9,
        risk_flags=risks,
    )


def test_explicit_task_takes_precedence_over_planner_and_matching() -> None:
    router = TaskRouter(_config())

    task, source, reason = router.select_task(
        explicit_task="documentation",
        planned_task="python-coding",
        prompt="Run Docker and change file.py",
        affected_paths=(Path("file.py"),),
    )

    assert task == "documentation"
    assert source is RouteSource.EXPLICIT
    assert "explicit" in reason


def test_planner_task_takes_precedence_over_matching() -> None:
    task, source, _ = TaskRouter(_config()).select_task(
        explicit_task=None,
        planned_task="python-coding",
        prompt="RUN DOCKER now",
        affected_paths=(Path("module.rs"),),
    )

    assert task == "python-coding"
    assert source is RouteSource.PLANNER


def test_phrase_matching_casefolds_and_first_declared_match_wins() -> None:
    task, source, reason = TaskRouter(_config()).select_task(
        explicit_task=None,
        planned_task=None,
        prompt="Please RUN DOCKER cleanup now",
        affected_paths=(),
    )

    assert task == "docker-invoke"
    assert source is RouteSource.MATCH
    assert "run docker" in reason


def test_file_glob_matching_and_default_selection() -> None:
    router = TaskRouter(_config())

    matched = router.select_task(
        explicit_task=None,
        planned_task=None,
        prompt="Implement this",
        affected_paths=(Path("src/native/example.cpp"),),
    )
    defaulted = router.select_task(
        explicit_task=None,
        planned_task=None,
        prompt="Investigate an unfamiliar issue",
        affected_paths=(),
    )

    assert matched[:2] == ("c-cpp-coding", RouteSource.MATCH)
    assert defaulted[:2] == ("bounded-diagnosis", RouteSource.DEFAULT)


def test_unknown_explicit_task_fails_without_fallback() -> None:
    with pytest.raises(UnknownTaskError, match="absent-task"):
        TaskRouter(_config()).select_task(
            explicit_task="absent-task",
            planned_task=None,
            prompt="anything",
            affected_paths=(),
        )


def test_security_hard_floor_raises_model_and_requires_approval() -> None:
    route = TaskRouter(_config()).route(
        _item(task="repository-search", risks=frozenset({"security"}))
    )

    assert route.model_alias == "frontier"
    assert route.model == _config().model_aliases["frontier"].model
    assert route.approval is ApprovalMode.REQUIRED
    assert route.sandbox is SandboxMode.READ_ONLY


def test_hard_floors_never_lower_existing_model_or_approval() -> None:
    route = TaskRouter(_config()).route(
        _item(task="security-sensitive", risks=frozenset({"destructive"}))
    )

    assert route.model_alias == "assurance"
    assert route.approval is ApprovalMode.REQUIRED
    assert route.destructive is True


def test_invalid_hard_floor_alias_fails_explicitly() -> None:
    config = _config()
    invalid = RouterConfig(
        schema_version=config.schema_version,
        default_task=config.default_task,
        planner_task=config.planner_task,
        completion_task=config.completion_task,
        maximum_read_concurrency=config.maximum_read_concurrency,
        repository_inspection_timeout_seconds=(
            config.repository_inspection_timeout_seconds
        ),
        model_aliases=MappingProxyType(
            {name: alias for name, alias in config.model_aliases.items() if name != "frontier"}
        ),
        tasks=config.tasks,
        matching=config.matching,
        hard_risk_rules=config.hard_risk_rules,
        digest=config.digest,
    )

    with pytest.raises(ConfigurationError, match="frontier"):
        TaskRouter(invalid).route(
            _item(task="repository-search", risks=frozenset({"security"}))
        )


def test_model_inventory_is_validated_without_downgrade() -> None:
    router = TaskRouter(_config())
    models = {alias.model for alias in _config().model_aliases.values()}

    router.validate_models(models)

    with pytest.raises(ModelUnavailableError, match=r"gpt-5\.6-sol"):
        router.validate_models({model for model in models if model != "gpt-5.6-sol"})
