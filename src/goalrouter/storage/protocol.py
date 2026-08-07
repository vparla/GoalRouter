# SPDX-License-Identifier: MIT
# File: src/goalrouter/storage/protocol.py
# Purpose: Structural interface for resumable run persistence

"""Run-store port consumed by application and scheduler services."""

from pathlib import Path
from typing import Protocol

from goalrouter.domain import RunEvent, RunState


class RunStoreProtocol(Protocol):
    """Asynchronous checkpoint, event, and report persistence."""

    async def create(self, state: RunState) -> None: ...

    async def load(
        self,
        run_id: str,
        *,
        expected_configuration_digest: str | None = None,
        acknowledge_configuration_change: bool = False,
    ) -> RunState: ...

    async def save(self, state: RunState) -> None: ...

    async def append_event(self, run_id: str, event: RunEvent) -> None: ...

    async def write_report(self, run_id: str, content: str) -> Path: ...
