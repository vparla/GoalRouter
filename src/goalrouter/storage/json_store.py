# SPDX-License-Identifier: MIT
# File: src/goalrouter/storage/json_store.py
# Purpose: Atomic JSON snapshots, append-only events, and local reports

"""Persist resumable runs beneath one explicit local state root."""

import asyncio
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

from goalrouter.domain import JsonValue, RunEvent, RunState
from goalrouter.errors import ResumeConfigurationChangedError, StateError
from goalrouter.run_ids import validate_run_id

_REDACTED = "[REDACTED]"
_SECRET_KEYS = {
    "apikey",
    "authorization",
    "bearer",
    "clientsecret",
    "openaikey",
    "openaiapikey",
    "password",
    "refreshtoken",
    "accesstoken",
    "secret",
}


class JsonRunStore:
    """Serialize one run at a time using atomic local snapshots."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._locks: dict[str, asyncio.Lock] = {}

    async def create(self, state: RunState) -> None:
        run_id = validate_run_id(state.run_id)
        async with self._lock_for(run_id):
            try:
                await asyncio.to_thread(self._create_sync, state)
            except OSError as error:
                raise StateError(f"Cannot create run {run_id}: {error}") from error

    async def load(
        self,
        run_id: str,
        *,
        expected_configuration_digest: str | None = None,
        acknowledge_configuration_change: bool = False,
    ) -> RunState:
        run_id = validate_run_id(run_id)
        async with self._lock_for(run_id):
            state = await asyncio.to_thread(self._load_sync, run_id)
        if (
            expected_configuration_digest is not None
            and state.configuration_digest != expected_configuration_digest
            and not acknowledge_configuration_change
        ):
            raise ResumeConfigurationChangedError(
                f"Run {run_id} uses configuration digest {state.configuration_digest}; "
                f"current digest is {expected_configuration_digest}"
            )
        return state

    async def save(self, state: RunState) -> None:
        run_id = validate_run_id(state.run_id)
        async with self._lock_for(run_id):
            try:
                await asyncio.to_thread(self._save_sync, state)
            except OSError as error:
                raise StateError(f"Cannot save run {run_id}: {error}") from error

    async def append_event(self, run_id: str, event: RunEvent) -> None:
        run_id = validate_run_id(run_id)
        async with self._lock_for(run_id):
            try:
                await asyncio.to_thread(self._append_event_sync, run_id, event)
            except OSError as error:
                raise StateError(f"Cannot append event for run {run_id}: {error}") from error

    async def write_report(self, run_id: str, content: str) -> Path:
        run_id = validate_run_id(run_id)
        async with self._lock_for(run_id):
            try:
                return await asyncio.to_thread(self._write_report_sync, run_id, content)
            except OSError as error:
                raise StateError(f"Cannot write report for run {run_id}: {error}") from error

    def _lock_for(self, run_id: str) -> asyncio.Lock:
        return self._locks.setdefault(run_id, asyncio.Lock())

    def _run_dir(self, run_id: str) -> Path:
        return self._root / run_id

    def _create_sync(self, state: RunState) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        run_dir = self._run_dir(state.run_id)
        run_dir.mkdir(exist_ok=False)
        (run_dir / "events.jsonl").write_text("", encoding="utf-8")
        _atomic_write_json(run_dir / "state.json", redact_json(state.to_dict()))

    def _load_sync(self, run_id: str) -> RunState:
        state_path = self._run_dir(run_id) / "state.json"
        try:
            loaded: object = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise StateError(f"Run {run_id} state is corrupt or unreadable: {error}") from error
        if not isinstance(loaded, Mapping) or not all(
            isinstance(key, str) for key in loaded
        ):
            raise StateError(f"Run {run_id} state is corrupt: expected a JSON object")
        raw = {str(key): value for key, value in loaded.items()}
        version = raw.get("schema_version")
        if version != 1:
            raise StateError(f"Unsupported state schema version {version!r} for run {run_id}")
        try:
            return RunState.from_dict(raw)
        except StateError as error:
            raise StateError(f"Run {run_id} state is corrupt: {error}") from error

    def _save_sync(self, state: RunState) -> None:
        run_dir = self._run_dir(state.run_id)
        if not run_dir.is_dir():
            raise FileNotFoundError(run_dir)
        _atomic_write_json(run_dir / "state.json", redact_json(state.to_dict()))

    def _append_event_sync(self, run_id: str, event: RunEvent) -> None:
        path = self._run_dir(run_id) / "events.jsonl"
        if not path.is_file():
            raise FileNotFoundError(path)
        line = json.dumps(redact_json(event.to_dict()), sort_keys=True, separators=(",", ":"))
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(f"{line}\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _write_report_sync(self, run_id: str, content: str) -> Path:
        path = self._run_dir(run_id) / "report.md"
        if not path.parent.is_dir():
            raise FileNotFoundError(path.parent)
        _atomic_write_text(path, content)
        return path


def redact_json(value: JsonValue) -> JsonValue:
    """Return a deep copy with known secret keys and key-shaped values removed."""

    if isinstance(value, dict):
        redacted: dict[str, JsonValue] = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
            redacted[key] = _REDACTED if normalized in _SECRET_KEYS else redact_json(item)
        return redacted
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    if isinstance(value, str) and (
        (value.startswith("sk-") and len(value) > 6)
        or value.casefold().startswith("bearer ")
    ):
        return _REDACTED
    return value


def _atomic_write_json(path: Path, value: JsonValue) -> None:
    content = json.dumps(value, indent=2, sort_keys=True) + "\n"
    _atomic_write_text(path, content)


def _atomic_write_text(path: Path, content: str) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
