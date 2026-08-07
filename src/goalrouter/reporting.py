# SPDX-License-Identifier: MIT
# File: src/goalrouter/reporting.py
# Purpose: Deterministic human-readable reporting for persisted runs

"""Render complete, locally persisted Markdown run reports."""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from goalrouter.domain import JsonValue, RunState
from goalrouter.storage.json_store import redact_json


class ReportRendererProtocol(Protocol):
    """Port for deterministic run-state rendering."""

    def render(self, state: RunState) -> str: ...


class ReportRenderer:
    """Render routes, execution evidence, and aggregate usage as Markdown."""

    def render(self, state: RunState) -> str:
        """Return a stable Markdown representation of the current run snapshot."""

        branch = (
            _display_text(state.repository.branch)
            if state.repository.branch
            else "(none)"
        )
        lines = [
            f"# GoalRouter run {state.run_id}",
            "",
            "## Objective",
            "",
            f"- Prompt: {state.objective.prompt}",
            f"- Project path: {_display_path(state.objective.project_path)}",
            f"- Status: {state.status.value}",
            f"- Created: {state.objective.created_at.isoformat()}",
            f"- Configuration: {_display_path(state.objective.config_path)}",
            f"- Configuration digest: {state.configuration_digest}",
            "",
            "## Repository",
            "",
            f"- Git worktree: {'yes' if state.repository.is_git_worktree else 'no'}",
            f"- Branch: {branch}",
            f"- Dirty state: {_dirty_summary(state)}",
            f"- Languages: {_pairs(state.repository.language_counts)}",
            f"- Docker files: {_paths(state.repository.docker_files)}",
            "",
            "### Instruction files",
            "",
        ]
        if state.repository.instruction_files:
            for instruction in state.repository.instruction_files:
                lines.extend(
                    [
                        f"#### {_display_path(instruction.path)}",
                        "",
                        instruction.content.rstrip(),
                        "",
                    ]
                )
        else:
            lines.extend(["(none)", ""])

        lines.extend(["## Work items", ""])
        for item_id in sorted(state.work_items):
            item = state.work_items[item_id]
            route = state.routes[item_id]
            result = state.results.get(item_id)
            lines.extend(
                [
                    f"### {item.id}: {item.title}",
                    "",
                    f"- Phase: {item.phase}",
                    f"- Task: {route.task}",
                    f"- Route reason: {route.reason}",
                    f"- Model: {route.model}",
                    f"- Reasoning effort: {route.reasoning_effort}",
                    f"- Sandbox: {route.sandbox.value}",
                    f"- Approval: {route.approval.value}",
                    f"- Dependencies: {', '.join(item.dependencies) or '(none)'}",
                    f"- Affected paths: {_paths(item.affected_paths)}",
                    f"- Required verification: {', '.join(item.verification) or '(none)'}",
                    f"- Status: {result.status.value if result else 'pending'}",
                    f"- Thread ID: {result.thread_id if result and result.thread_id else '(none)'}",
                    f"- Turn ID: {result.turn_id if result and result.turn_id else '(none)'}",
                ]
            )
            if result is None:
                lines.append("")
                continue
            lines.extend(
                [
                    f"- Usage: input {result.input_tokens}, cached "
                    f"{result.cached_input_tokens}, output {result.output_tokens}",
                    f"- Duration seconds: {result.duration_seconds:g}",
                    f"- Changed paths: {_paths(result.changed_paths)}",
                    f"- Verification evidence: {', '.join(result.verification) or '(none)'}",
                    f"- Confidence: {result.confidence:g}",
                    f"- Error: {result.error or '(none)'}",
                    "",
                    "#### Final response",
                    "",
                    result.final_response or "(none)",
                    "",
                    "#### Safe SDK item summary",
                    "",
                    _safe_json(list(result.sdk_items)),
                    "",
                ]
            )

        input_tokens = sum(result.input_tokens for result in state.results.values())
        cached_tokens = sum(result.cached_input_tokens for result in state.results.values())
        output_tokens = sum(result.output_tokens for result in state.results.values())
        duration = sum(result.duration_seconds for result in state.results.values())
        lines.extend(
            [
                "## Total usage",
                "",
                f"- Input tokens: {input_tokens}",
                f"- Cached input tokens: {cached_tokens}",
                f"- Output tokens: {output_tokens}",
                f"- Duration seconds: {duration:g}",
                "",
            ]
        )
        return "\n".join(lines)


def _dirty_summary(state: RunState) -> str:
    if not state.repository.dirty_paths:
        return "clean"
    return ", ".join(_display_path(path) for path in state.repository.dirty_paths)


def _paths(paths: Sequence[Path]) -> str:
    return ", ".join(_display_path(path) for path in paths) or "(none)"


def _display_path(path: Path) -> str:
    return _display_text(path.as_posix())


def _display_text(value: str) -> str:
    output: list[str] = []
    for character in value:
        codepoint = ord(character)
        if character == "\\":
            output.append("\\\\")
        elif character == "\n":
            output.append("\\n")
        elif character == "\r":
            output.append("\\r")
        elif character == "\t":
            output.append("\\t")
        elif 0xDC80 <= codepoint <= 0xDCFF:
            output.append(f"\\x{codepoint - 0xDC00:02x}")
        elif codepoint < 0x20 or codepoint == 0x7F:
            output.append(f"\\x{codepoint:02x}")
        elif 0xD800 <= codepoint <= 0xDFFF:
            output.append(f"\\u{codepoint:04x}")
        else:
            output.append(character)
    return "".join(output)


def _pairs(values: tuple[tuple[str, int], ...]) -> str:
    return ", ".join(f"{name}={count}" for name, count in values) or "(none)"


def _safe_json(value: JsonValue) -> str:
    return json.dumps(redact_json(value), sort_keys=True, separators=(",", ":"))
