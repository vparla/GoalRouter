# SPDX-License-Identifier: MIT
# File: src/goalrouter/routing.py
# Purpose: Deterministic YAML-driven task selection and route resolution

"""Resolve work items through ordered, repository-neutral routing policy."""

from collections.abc import Collection, Sequence
from fnmatch import fnmatchcase
from pathlib import Path

from goalrouter.config import RouterConfig
from goalrouter.domain import (
    ApprovalMode,
    ModelAlias,
    RouteDecision,
    RouteSource,
    WorkItem,
)
from goalrouter.errors import ConfigurationError, ModelUnavailableError, UnknownTaskError


class TaskRouter:
    """Select configured tasks and enforce global hard-risk floors."""

    def __init__(self, config: RouterConfig) -> None:
        self._config = config

    def select_task(
        self,
        *,
        explicit_task: str | None,
        planned_task: str | None,
        prompt: str,
        affected_paths: Sequence[Path],
    ) -> tuple[str, RouteSource, str]:
        """Apply explicit, planner, ordered matcher, then default precedence."""

        if explicit_task is not None:
            self._require_task(explicit_task)
            return explicit_task, RouteSource.EXPLICIT, f"explicit task {explicit_task!r}"
        if planned_task is not None:
            self._require_task(planned_task)
            return planned_task, RouteSource.PLANNER, f"planner task {planned_task!r}"

        folded_prompt = prompt.casefold()
        for index, rule in enumerate(self._config.matching):
            phrase = next(
                (candidate for candidate in rule.phrases if candidate.casefold() in folded_prompt),
                None,
            )
            if phrase is not None:
                return (
                    rule.task,
                    RouteSource.MATCH,
                    f"matching rule {index} phrase {phrase!r}",
                )
            glob_match = _first_glob_match(affected_paths, rule.file_globs)
            if glob_match is not None:
                path, pattern = glob_match
                return (
                    rule.task,
                    RouteSource.MATCH,
                    f"matching rule {index} glob {pattern!r} matched {path.as_posix()!r}",
                )

        self._require_task(self._config.default_task)
        return (
            self._config.default_task,
            RouteSource.DEFAULT,
            f"default task {self._config.default_task!r}",
        )

    def route(self, item: WorkItem, *, explicit_task: str | None = None) -> RouteDecision:
        """Resolve one work item to concrete model and execution policy."""

        task_name, source, reason = self.select_task(
            explicit_task=explicit_task,
            planned_task=item.task,
            prompt=item.instructions,
            affected_paths=item.affected_paths,
        )
        policy = self._config.tasks[task_name]
        original_alias = self._require_alias(policy.model_alias)
        minimum_rank = original_alias.rank
        approval = policy.approval
        active_flags = set(item.risk_flags)
        if policy.destructive:
            active_flags.add("destructive")
        if policy.external_write:
            active_flags.add("external-write")

        applied_floors: list[str] = []
        for rule in self._config.hard_risk_rules:
            if rule.flag not in active_flags:
                continue
            if rule.minimum_model_alias is not None:
                minimum = self._require_alias(rule.minimum_model_alias)
                minimum_rank = max(minimum_rank, minimum.rank)
                applied_floors.append(f"{rule.flag}:{rule.minimum_model_alias}")
            if rule.approval is ApprovalMode.REQUIRED:
                approval = ApprovalMode.REQUIRED

        candidates = [
            alias
            for alias in self._config.model_aliases.values()
            if alias.rank >= minimum_rank
        ]
        if not candidates:
            raise ConfigurationError(
                f"No configured model alias satisfies minimum rank {minimum_rank}"
            )
        selected_alias = min(candidates, key=lambda alias: alias.rank)
        if applied_floors:
            reason = f"{reason}; hard floors {', '.join(applied_floors)}"

        return RouteDecision(
            task=task_name,
            model_alias=selected_alias.name,
            model=selected_alias.model,
            reasoning_effort=selected_alias.reasoning_effort,
            sandbox=policy.sandbox,
            approval=approval,
            timeout_seconds=policy.timeout_seconds,
            max_attempts=policy.max_attempts,
            destructive=policy.destructive or "destructive" in item.risk_flags,
            external_write=policy.external_write or "external-write" in item.risk_flags,
            escalation_task=policy.escalate_to,
            source=source,
            reason=reason,
        )

    def validate_models(self, available_model_ids: Collection[str]) -> None:
        """Fail if any configured concrete model is absent; never downgrade."""

        missing = sorted(
            {
                alias.model
                for alias in self._config.model_aliases.values()
                if alias.model not in available_model_ids
            }
        )
        if missing:
            raise ModelUnavailableError(
                f"Configured model unavailable: {', '.join(missing)}"
            )

    def _require_task(self, task: str) -> None:
        if task not in self._config.tasks:
            raise UnknownTaskError(f"Unknown task {task!r}")

    def _require_alias(self, name: str) -> ModelAlias:
        try:
            return self._config.model_aliases[name]
        except KeyError as error:
            raise ConfigurationError(f"Unknown model alias {name!r}") from error


def _first_glob_match(
    paths: Sequence[Path], patterns: Sequence[str]
) -> tuple[Path, str] | None:
    for path in paths:
        normalized = path.as_posix()
        for pattern in patterns:
            if fnmatchcase(normalized, pattern) or (
                pattern.startswith("**/") and fnmatchcase(normalized, pattern[3:])
            ):
                return path, pattern
    return None
