# SPDX-License-Identifier: MIT
# File: src/goalrouter/config.py
# Purpose: Safe routing YAML loading and semantic validation

"""Load and validate repository-neutral task-to-model routing policy."""

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from goalrouter.domain import (
    ApprovalMode,
    HardRiskRule,
    MatchRule,
    ModelAlias,
    SandboxMode,
    TaskPolicy,
)
from goalrouter.errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class RouterConfig:
    """Validated routing policy with a source-content digest."""

    schema_version: int
    default_task: str
    planner_task: str
    completion_task: str
    maximum_read_concurrency: int
    repository_inspection_timeout_seconds: int
    model_aliases: Mapping[str, ModelAlias]
    tasks: Mapping[str, TaskPolicy]
    matching: tuple[MatchRule, ...]
    hard_risk_rules: tuple[HardRiskRule, ...]
    digest: str


def load_router_config(path: Path, *, schema_path: Path | None = None) -> RouterConfig:
    """Load one YAML policy safely, then validate structure and references."""

    try:
        raw_bytes = path.read_bytes()
        loaded: object = yaml.safe_load(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ConfigurationError(f"Cannot load routing configuration {path}: {error}") from error
    raw = _mapping(loaded, f"routing configuration {path}")
    resolved_schema = schema_path or Path(
        os.environ.get(
            "GOALROUTER_SCHEMA", "/etc/goalrouter/task-models.schema.json"
        )
    )
    validate_json_schema(raw, schema_path=resolved_schema, config_path=path)
    _validate_semantics(raw, config_path=path)
    return _parse_router_config(raw, digest=sha256(raw_bytes).hexdigest())


def validate_json_schema(
    raw: Mapping[str, object], *, schema_path: Path, config_path: Path
) -> None:
    """Validate raw YAML data against the shipped Draft 2020-12 schema."""

    try:
        schema_loaded: object = json.loads(schema_path.read_text(encoding="utf-8"))
        schema = _mapping(schema_loaded, f"schema {schema_path}")
        validator = Draft202012Validator(schema)
        error = next(iter(validator.iter_errors(raw)), None)
    except (OSError, UnicodeError, json.JSONDecodeError, SchemaError) as failure:
        raise ConfigurationError(
            f"Cannot load routing schema {schema_path}: {failure}"
        ) from failure
    if error is not None:
        location = ".".join(str(part) for part in error.absolute_path) or "root"
        raise ConfigurationError(
            f"Invalid routing configuration {config_path} at {location}: {error.message}"
        )


def _validate_semantics(raw: Mapping[str, object], *, config_path: Path) -> None:
    aliases = _mapping(_field(raw, "model-aliases"), "model-aliases")
    tasks = _mapping(_field(raw, "tasks"), "tasks")
    ranks: dict[int, str] = {}
    for name, alias_value in aliases.items():
        alias = _mapping(alias_value, f"model-aliases.{name}")
        rank = _integer(alias, "rank")
        if rank <= 0:
            raise ConfigurationError(f"Invalid model alias rank for {name} in {config_path}")
        if rank in ranks:
            raise ConfigurationError(
                f"Duplicate model alias rank {rank} for {ranks[rank]} and {name} in {config_path}"
            )
        ranks[rank] = name

    for field in ("default-task", "planner-task", "completion-task"):
        task_name = _string(raw, field)
        if task_name not in tasks:
            raise ConfigurationError(f"Unknown {field} {task_name!r} in {config_path}")

    for name, task_value in tasks.items():
        task = _mapping(task_value, f"tasks.{name}")
        alias_name = _string(task, "model-alias")
        if alias_name not in aliases:
            raise ConfigurationError(
                f"Task {name!r} references unknown model alias {alias_name!r} in {config_path}"
            )
        escalation = _optional_string(task, "escalate-to")
        if escalation is not None and escalation not in tasks:
            raise ConfigurationError(
                f"Task {name!r} references unknown escalation {escalation!r} in {config_path}"
            )

    for index, rule_value in enumerate(_optional_sequence(raw, "matching")):
        rule = _mapping(rule_value, f"matching[{index}]")
        task_name = _string(rule, "task")
        if task_name not in tasks:
            raise ConfigurationError(
                f"Matching rule {index} references unknown task {task_name!r} in {config_path}"
            )

    for index, rule_value in enumerate(_optional_sequence(raw, "hard-risk-rules")):
        rule = _mapping(rule_value, f"hard-risk-rules[{index}]")
        risk_alias = _optional_string(rule, "minimum-model-alias")
        if risk_alias is not None and risk_alias not in aliases:
            raise ConfigurationError(
                f"Hard-risk rule {index} references unknown alias {risk_alias!r} in {config_path}"
            )


def _parse_router_config(raw: Mapping[str, object], *, digest: str) -> RouterConfig:
    aliases_raw = _mapping(_field(raw, "model-aliases"), "model-aliases")
    tasks_raw = _mapping(_field(raw, "tasks"), "tasks")
    aliases = {
        name: _parse_model_alias(name, _mapping(value, f"model-aliases.{name}"))
        for name, value in aliases_raw.items()
    }
    tasks = {
        name: _parse_task_policy(name, _mapping(value, f"tasks.{name}"))
        for name, value in tasks_raw.items()
    }
    matching = tuple(
        _parse_match_rule(_mapping(value, f"matching[{index}]"))
        for index, value in enumerate(_optional_sequence(raw, "matching"))
    )
    hard_risk_rules = tuple(
        _parse_hard_risk_rule(_mapping(value, f"hard-risk-rules[{index}]"))
        for index, value in enumerate(_optional_sequence(raw, "hard-risk-rules"))
    )
    return RouterConfig(
        schema_version=_integer(raw, "schema-version"),
        default_task=_string(raw, "default-task"),
        planner_task=_string(raw, "planner-task"),
        completion_task=_string(raw, "completion-task"),
        maximum_read_concurrency=_integer(raw, "maximum-read-concurrency"),
        repository_inspection_timeout_seconds=_integer(
            raw, "repository-inspection-timeout-seconds"
        ),
        model_aliases=MappingProxyType(aliases),
        tasks=MappingProxyType(tasks),
        matching=matching,
        hard_risk_rules=hard_risk_rules,
        digest=digest,
    )


def _parse_model_alias(name: str, raw: Mapping[str, object]) -> ModelAlias:
    return ModelAlias(
        name=name,
        model=_string(raw, "model"),
        reasoning_effort=_string(raw, "reasoning-effort"),
        rank=_integer(raw, "rank"),
    )


def _parse_task_policy(name: str, raw: Mapping[str, object]) -> TaskPolicy:
    return TaskPolicy(
        task=name,
        description=_string(raw, "description"),
        model_alias=_string(raw, "model-alias"),
        sandbox=SandboxMode(_string(raw, "sandbox")),
        approval=ApprovalMode(_string(raw, "approval")),
        timeout_seconds=_integer(raw, "timeout-seconds"),
        max_attempts=_integer(raw, "max-attempts"),
        destructive=_optional_boolean(raw, "destructive", default=False),
        external_write=_optional_boolean(raw, "external-write", default=False),
        escalate_to=_optional_string(raw, "escalate-to"),
    )


def _parse_match_rule(raw: Mapping[str, object]) -> MatchRule:
    return MatchRule(
        task=_string(raw, "task"),
        phrases=tuple(_strings(_optional_sequence(raw, "phrases"), "phrases")),
        file_globs=tuple(_strings(_optional_sequence(raw, "file-globs"), "file-globs")),
    )


def _parse_hard_risk_rule(raw: Mapping[str, object]) -> HardRiskRule:
    approval = _optional_string(raw, "approval")
    return HardRiskRule(
        flag=_string(raw, "flag"),
        minimum_model_alias=_optional_string(raw, "minimum-model-alias"),
        approval=ApprovalMode(approval) if approval is not None else None,
    )


def _field(raw: Mapping[str, object], name: str) -> object:
    if name not in raw:
        raise ConfigurationError(f"Missing configuration field: {name}")
    return raw[name]


def _mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ConfigurationError(f"Expected object for {context}")
    return {str(key): item for key, item in value.items()}


def _string(raw: Mapping[str, object], name: str) -> str:
    value = _field(raw, name)
    if not isinstance(value, str):
        raise ConfigurationError(f"Expected string for {name}")
    return value


def _optional_string(raw: Mapping[str, object], name: str) -> str | None:
    value = raw.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigurationError(f"Expected string for {name}")
    return value


def _integer(raw: Mapping[str, object], name: str) -> int:
    value = _field(raw, name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"Expected integer for {name}")
    return value


def _optional_boolean(raw: Mapping[str, object], name: str, *, default: bool) -> bool:
    value = raw.get(name, default)
    if not isinstance(value, bool):
        raise ConfigurationError(f"Expected boolean for {name}")
    return value


def _optional_sequence(raw: Mapping[str, object], name: str) -> Sequence[object]:
    value = raw.get(name, ())
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise ConfigurationError(f"Expected array for {name}")
    return value


def _strings(values: Sequence[object], context: str) -> tuple[str, ...]:
    if not all(isinstance(value, str) for value in values):
        raise ConfigurationError(f"Expected strings for {context}")
    return tuple(str(value) for value in values)
