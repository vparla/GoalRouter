# SPDX-License-Identifier: MIT
# File: src/goalrouter/sdk/protocol.py
# Purpose: SDK-independent structural interface for Codex execution

"""Structural Codex client interface consumed by GoalRouter services."""

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from goalrouter.domain import RouteDecision, WorkResult


class CodexClientProtocol(Protocol):
    """Port implemented by the concrete AsyncCodex adapter and test fakes."""

    async def available_models(self) -> frozenset[str]: ...

    async def run_new_thread(
        self,
        *,
        project_path: Path,
        route: RouteDecision,
        prompt: str,
        developer_instructions: str,
        output_schema: Mapping[str, object] | None = None,
    ) -> WorkResult: ...

    async def resume_thread(
        self,
        *,
        thread_id: str,
        project_path: Path,
        route: RouteDecision,
        prompt: str,
        output_schema: Mapping[str, object] | None = None,
    ) -> WorkResult: ...
