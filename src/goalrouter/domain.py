# SPDX-License-Identifier: MIT
# File: src/goalrouter/domain.py
# Purpose: SDK-independent GoalRouter domain values and state serialization

"""SDK-independent domain values for planning, routing, and persisted runs."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from goalrouter.errors import StateError

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


class AuthMode(StrEnum):
    """Explicit Codex authentication selection."""

    EXISTING_SESSION = "existing-session"
    API_KEY = "api-key"


class SandboxMode(StrEnum):
    """Supported Codex sandbox presets."""

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"


class ApprovalMode(StrEnum):
    """Controller-side dispatch approval policy."""

    AUTOMATIC = "automatic"
    REQUIRED = "required"


class AccessMode(StrEnum):
    """Declared work-item filesystem intent."""

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"


class WorkStatus(StrEnum):
    """Terminal and non-terminal work-item states."""

    PENDING = "pending"
    AWAITING_APPROVAL = "awaiting-approval"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"


class RunStatus(StrEnum):
    """Overall run lifecycle state."""

    PLANNED = "planned"
    AWAITING_APPROVAL = "awaiting-approval"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class RouteSource(StrEnum):
    """Routing-precedence level that selected a task."""

    EXPLICIT = "explicit"
    PLANNER = "planner"
    MATCH = "match"
    DEFAULT = "default"


@dataclass(frozen=True, slots=True)
class InstructionFile:
    """Applicable repository instruction content."""

    path: Path
    content: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {"path": str(self.path), "content": self.content}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> InstructionFile:
        return cls(path=Path(_string(value, "path")), content=_string(value, "content"))


@dataclass(frozen=True, slots=True)
class Objective:
    """A user objective and its immutable execution context."""

    id: str
    prompt: str
    project_path: Path
    explicit_task: str | None
    config_path: Path
    created_at: datetime

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "project_path": str(self.project_path),
            "explicit_task": self.explicit_task,
            "config_path": str(self.config_path),
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Objective:
        return cls(
            id=_string(value, "id"),
            prompt=_string(value, "prompt"),
            project_path=Path(_string(value, "project_path")),
            explicit_task=_optional_string(value, "explicit_task"),
            config_path=Path(_string(value, "config_path")),
            created_at=_datetime(value, "created_at"),
        )


@dataclass(frozen=True, slots=True)
class RepositoryContext:
    """Read-only evidence discovered for a target project."""

    project_path: Path
    is_git_worktree: bool
    branch: str | None
    dirty_paths: tuple[Path, ...]
    instruction_files: tuple[InstructionFile, ...]
    language_counts: tuple[tuple[str, int], ...]
    docker_files: tuple[Path, ...]
    command_errors: tuple[str, ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "project_path": str(self.project_path),
            "is_git_worktree": self.is_git_worktree,
            "branch": self.branch,
            "dirty_paths": [str(path) for path in self.dirty_paths],
            "instruction_files": [item.to_dict() for item in self.instruction_files],
            "language_counts": [
                {"language": language, "count": count}
                for language, count in self.language_counts
            ],
            "docker_files": [str(path) for path in self.docker_files],
            "command_errors": list(self.command_errors),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> RepositoryContext:
        instructions = tuple(
            InstructionFile.from_dict(_mapping(item, "instruction_files item"))
            for item in _sequence(value, "instruction_files")
        )
        language_counts = tuple(
            (
                _string(_mapping(item, "language_counts item"), "language"),
                _integer(_mapping(item, "language_counts item"), "count"),
            )
            for item in _sequence(value, "language_counts")
        )
        return cls(
            project_path=Path(_string(value, "project_path")),
            is_git_worktree=_boolean(value, "is_git_worktree"),
            branch=_optional_string(value, "branch"),
            dirty_paths=tuple(
                Path(_plain_string(item, "dirty_paths item"))
                for item in _sequence(value, "dirty_paths")
            ),
            instruction_files=instructions,
            language_counts=language_counts,
            docker_files=tuple(
                Path(_plain_string(item, "docker_files item"))
                for item in _sequence(value, "docker_files")
            ),
            command_errors=tuple(
                _plain_string(item, "command_errors item")
                for item in _sequence(value, "command_errors")
            ),
        )


@dataclass(frozen=True, slots=True)
class ModelAlias:
    """A configured model role and its hard-floor rank."""

    name: str
    model: str
    reasoning_effort: str
    rank: int


@dataclass(frozen=True, slots=True)
class TaskPolicy:
    """A fully parsed task entry from routing configuration."""

    task: str
    description: str
    model_alias: str
    sandbox: SandboxMode
    approval: ApprovalMode
    timeout_seconds: int
    max_attempts: int
    destructive: bool
    external_write: bool
    escalate_to: str | None


@dataclass(frozen=True, slots=True)
class MatchRule:
    """An ordered repository-neutral task matcher."""

    task: str
    phrases: tuple[str, ...]
    file_globs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HardRiskRule:
    """A global minimum model and approval constraint for a risk flag."""

    flag: str
    minimum_model_alias: str | None
    approval: ApprovalMode | None


@dataclass(frozen=True, slots=True)
class WorkItem:
    """One bounded, dependency-addressable phase of an objective."""

    id: str
    title: str
    instructions: str
    task: str
    phase: str
    dependencies: tuple[str, ...]
    access: AccessMode
    affected_paths: tuple[Path, ...]
    expected_result: str
    verification: tuple[str, ...]
    confidence: float
    risk_flags: frozenset[str]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "id": self.id,
            "title": self.title,
            "instructions": self.instructions,
            "task": self.task,
            "phase": self.phase,
            "dependencies": list(self.dependencies),
            "access": self.access.value,
            "affected_paths": [str(path) for path in self.affected_paths],
            "expected_result": self.expected_result,
            "verification": list(self.verification),
            "confidence": self.confidence,
            "risk_flags": list[JsonValue](sorted(self.risk_flags)),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> WorkItem:
        return cls(
            id=_string(value, "id"),
            title=_string(value, "title"),
            instructions=_string(value, "instructions"),
            task=_string(value, "task"),
            phase=_string(value, "phase"),
            dependencies=tuple(
                _plain_string(item, "dependencies item")
                for item in _sequence(value, "dependencies")
            ),
            access=_enum(AccessMode, value, "access"),
            affected_paths=tuple(
                Path(_plain_string(item, "affected_paths item"))
                for item in _sequence(value, "affected_paths")
            ),
            expected_result=_string(value, "expected_result"),
            verification=tuple(
                _plain_string(item, "verification item")
                for item in _sequence(value, "verification")
            ),
            confidence=_number(value, "confidence"),
            risk_flags=frozenset(
                _plain_string(item, "risk_flags item")
                for item in _sequence(value, "risk_flags")
            ),
        )


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """A complete auditable routing-policy resolution."""

    task: str
    model_alias: str
    model: str
    reasoning_effort: str
    sandbox: SandboxMode
    approval: ApprovalMode
    timeout_seconds: int
    max_attempts: int
    destructive: bool
    external_write: bool
    escalation_task: str | None
    source: RouteSource
    reason: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "task": self.task,
            "model_alias": self.model_alias,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "sandbox": self.sandbox.value,
            "approval": self.approval.value,
            "timeout_seconds": self.timeout_seconds,
            "max_attempts": self.max_attempts,
            "destructive": self.destructive,
            "external_write": self.external_write,
            "escalation_task": self.escalation_task,
            "source": self.source.value,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> RouteDecision:
        return cls(
            task=_string(value, "task"),
            model_alias=_string(value, "model_alias"),
            model=_string(value, "model"),
            reasoning_effort=_string(value, "reasoning_effort"),
            sandbox=_enum(SandboxMode, value, "sandbox"),
            approval=_enum(ApprovalMode, value, "approval"),
            timeout_seconds=_integer(value, "timeout_seconds"),
            max_attempts=_integer(value, "max_attempts"),
            destructive=_boolean(value, "destructive"),
            external_write=_boolean(value, "external_write"),
            escalation_task=_optional_string(value, "escalation_task"),
            source=_enum(RouteSource, value, "source"),
            reason=_string(value, "reason"),
        )


@dataclass(frozen=True, slots=True)
class WorkResult:
    """Normalized output and evidence from one Codex turn."""

    work_item_id: str
    thread_id: str | None
    turn_id: str | None
    status: WorkStatus
    final_response: str | None
    sdk_items: tuple[JsonValue, ...]
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    duration_seconds: float
    changed_paths: tuple[Path, ...]
    verification: tuple[str, ...]
    confidence: float
    escalation_requested: bool
    error: str | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "work_item_id": self.work_item_id,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "status": self.status.value,
            "final_response": self.final_response,
            "sdk_items": list(self.sdk_items),
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "duration_seconds": self.duration_seconds,
            "changed_paths": [str(path) for path in self.changed_paths],
            "verification": list(self.verification),
            "confidence": self.confidence,
            "escalation_requested": self.escalation_requested,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> WorkResult:
        return cls(
            work_item_id=_string(value, "work_item_id"),
            thread_id=_optional_string(value, "thread_id"),
            turn_id=_optional_string(value, "turn_id"),
            status=_enum(WorkStatus, value, "status"),
            final_response=_optional_string(value, "final_response"),
            sdk_items=tuple(_json_value(item) for item in _sequence(value, "sdk_items")),
            input_tokens=_integer(value, "input_tokens"),
            cached_input_tokens=_integer(value, "cached_input_tokens"),
            output_tokens=_integer(value, "output_tokens"),
            duration_seconds=_number(value, "duration_seconds"),
            changed_paths=tuple(
                Path(_plain_string(item, "changed_paths item"))
                for item in _sequence(value, "changed_paths")
            ),
            verification=tuple(
                _plain_string(item, "verification item")
                for item in _sequence(value, "verification")
            ),
            confidence=_number(value, "confidence"),
            escalation_requested=_boolean(value, "escalation_requested"),
            error=_optional_string(value, "error"),
        )


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """An explicit approval bound to state and route fingerprints."""

    run_id: str
    work_item_id: str
    approved_by: str
    approved_at: datetime
    configuration_digest: str
    fingerprint: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "run_id": self.run_id,
            "work_item_id": self.work_item_id,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at.isoformat(),
            "configuration_digest": self.configuration_digest,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ApprovalRecord:
        return cls(
            run_id=_string(value, "run_id"),
            work_item_id=_string(value, "work_item_id"),
            approved_by=_string(value, "approved_by"),
            approved_at=_datetime(value, "approved_at"),
            configuration_digest=_string(value, "configuration_digest"),
            fingerprint=_string(value, "fingerprint"),
        )


@dataclass(frozen=True, slots=True)
class RunEvent:
    """One append-only persisted run-state transition."""

    timestamp: datetime
    event: str
    work_item_id: str | None
    details: Mapping[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "event": self.event,
            "work_item_id": self.work_item_id,
            "details": dict(self.details),
        }


@dataclass(slots=True)
class RunState:
    """Mutable aggregate checkpoint for one resumable objective."""

    schema_version: int
    configuration_digest: str
    objective: Objective
    repository: RepositoryContext
    work_items: dict[str, WorkItem]
    routes: dict[str, RouteDecision]
    results: dict[str, WorkResult]
    approvals: dict[str, ApprovalRecord]
    status: RunStatus

    @property
    def run_id(self) -> str:
        return self.objective.id

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "configuration_digest": self.configuration_digest,
            "objective": self.objective.to_dict(),
            "repository": self.repository.to_dict(),
            "work_items": {
                key: value.to_dict() for key, value in sorted(self.work_items.items())
            },
            "routes": {key: value.to_dict() for key, value in sorted(self.routes.items())},
            "results": {key: value.to_dict() for key, value in sorted(self.results.items())},
            "approvals": {
                key: value.to_dict() for key, value in sorted(self.approvals.items())
            },
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> RunState:
        try:
            work_items = {
                key: WorkItem.from_dict(_mapping(item, f"work_items.{key}"))
                for key, item in _string_mapping(value, "work_items").items()
            }
            routes = {
                key: RouteDecision.from_dict(_mapping(item, f"routes.{key}"))
                for key, item in _string_mapping(value, "routes").items()
            }
            results = {
                key: WorkResult.from_dict(_mapping(item, f"results.{key}"))
                for key, item in _string_mapping(value, "results").items()
            }
            approvals = {
                key: ApprovalRecord.from_dict(_mapping(item, f"approvals.{key}"))
                for key, item in _string_mapping(value, "approvals").items()
            }
            return cls(
                schema_version=_integer(value, "schema_version"),
                configuration_digest=_string(value, "configuration_digest"),
                objective=Objective.from_dict(_field_mapping(value, "objective")),
                repository=RepositoryContext.from_dict(_field_mapping(value, "repository")),
                work_items=work_items,
                routes=routes,
                results=results,
                approvals=approvals,
                status=_enum(RunStatus, value, "status"),
            )
        except StateError:
            raise
        except (TypeError, ValueError) as error:
            raise StateError(f"Invalid run state: {error}") from error


def _field(value: Mapping[str, object], name: str) -> object:
    if name not in value:
        raise StateError(f"Missing run-state field: {name}")
    return value[name]


def _plain_string(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise StateError(f"Expected string for {context}")
    return value


def _string(value: Mapping[str, object], name: str) -> str:
    return _plain_string(_field(value, name), name)


def _optional_string(value: Mapping[str, object], name: str) -> str | None:
    item = _field(value, name)
    if item is None:
        return None
    return _plain_string(item, name)


def _boolean(value: Mapping[str, object], name: str) -> bool:
    item = _field(value, name)
    if not isinstance(item, bool):
        raise StateError(f"Expected boolean for {name}")
    return item


def _integer(value: Mapping[str, object], name: str) -> int:
    item = _field(value, name)
    if isinstance(item, bool) or not isinstance(item, int):
        raise StateError(f"Expected integer for {name}")
    return item


def _number(value: Mapping[str, object], name: str) -> float:
    item = _field(value, name)
    if isinstance(item, bool) or not isinstance(item, int | float):
        raise StateError(f"Expected number for {name}")
    return float(item)


def _sequence(value: Mapping[str, object], name: str) -> Sequence[object]:
    item = _field(value, name)
    if isinstance(item, str | bytes) or not isinstance(item, Sequence):
        raise StateError(f"Expected array for {name}")
    return item


def _mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise StateError(f"Expected object for {context}")
    return {str(key): item for key, item in value.items()}


def _field_mapping(value: Mapping[str, object], name: str) -> Mapping[str, object]:
    return _mapping(_field(value, name), name)


def _string_mapping(value: Mapping[str, object], name: str) -> Mapping[str, object]:
    return _field_mapping(value, name)


def _datetime(value: Mapping[str, object], name: str) -> datetime:
    raw = _string(value, name)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as error:
        raise StateError(f"Expected ISO datetime for {name}") from error
    if parsed.tzinfo is None:
        raise StateError(f"Expected timezone-aware datetime for {name}")
    return parsed


def _enum[T: StrEnum](enum_type: type[T], value: Mapping[str, object], name: str) -> T:
    raw = _string(value, name)
    try:
        return enum_type(raw)
    except ValueError as error:
        raise StateError(f"Unknown {name}: {raw}") from error


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping) and all(isinstance(key, str) for key in value):
        return {str(key): _json_value(item) for key, item in value.items()}
    raise StateError("SDK item contains a non-JSON value")
