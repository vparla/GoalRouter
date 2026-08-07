# SPDX-License-Identifier: MIT
# File: tests/unit/test_sdk_adapter.py
# Purpose: Verify AsyncCodex adapter mapping, authentication, and normalization

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Self

import pytest

from goalrouter.domain import (
    ApprovalMode,
    AuthMode,
    RouteDecision,
    RouteSource,
    SandboxMode,
    WorkStatus,
)
from goalrouter.errors import AuthenticationError, SdkError, TurnTimeoutError
from goalrouter.sdk.codex import CodexSdkClient


def _route() -> RouteDecision:
    return RouteDecision(
        task="repository-search",
        model_alias="economy",
        model="example-model",
        reasoning_effort="low",
        sandbox=SandboxMode.READ_ONLY,
        approval=ApprovalMode.AUTOMATIC,
        timeout_seconds=30,
        max_attempts=1,
        destructive=False,
        external_write=False,
        escalation_task=None,
        source=RouteSource.EXPLICIT,
        reason="explicit task",
    )


def _turn(*, final_response: str | None = "done") -> SimpleNamespace:
    usage = SimpleNamespace(
        total=SimpleNamespace(
            input_tokens=100,
            cached_input_tokens=25,
            output_tokens=50,
            reasoning_output_tokens=10,
            total_tokens=150,
        )
    )
    return SimpleNamespace(
        id="turn-1",
        status=SimpleNamespace(value="completed"),
        error=None,
        started_at=1000,
        completed_at=2500,
        duration_ms=1500,
        final_response=final_response,
        items=[{"type": "message", "text": "safe"}],
        usage=usage,
    )


class FakeThread:
    def __init__(self, *, thread_id: str, turn: SimpleNamespace, delay: float = 0) -> None:
        self.id = thread_id
        self.turn_result = turn
        self.delay = delay
        self.run_calls: list[dict[str, object]] = []

    async def run(
        self,
        prompt: str,
        *,
        effort: object = None,
        output_schema: Mapping[str, object] | None = None,
    ) -> SimpleNamespace:
        self.run_calls.append(
            {"prompt": prompt, "effort": effort, "output_schema": output_schema}
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.turn_result


class FakeAsyncCodex:
    def __init__(self, *, turn: SimpleNamespace | None = None, delay: float = 0) -> None:
        self.enters = 0
        self.exits = 0
        self.account_calls = 0
        self.login_calls: list[str] = []
        self.model_response = SimpleNamespace(
            data=[SimpleNamespace(id="example-model"), SimpleNamespace(id="other-model")]
        )
        self.account_response = SimpleNamespace(account=object(), requires_openai_auth=False)
        self.thread = FakeThread(
            thread_id="thread-new", turn=turn or _turn(), delay=delay
        )
        self.start_calls: list[dict[str, object]] = []
        self.resume_calls: list[dict[str, object]] = []

    async def __aenter__(self) -> Self:
        self.enters += 1
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.exits += 1

    async def account(self, *, refresh_token: bool = False) -> SimpleNamespace:
        self.account_calls += 1
        return self.account_response

    async def login_api_key(self, api_key: str) -> None:
        self.login_calls.append(api_key)

    async def models(self, *, include_hidden: bool = False) -> SimpleNamespace:
        return self.model_response

    async def thread_start(self, **kwargs: object) -> FakeThread:
        self.start_calls.append(kwargs)
        return self.thread

    async def thread_resume(self, thread_id: str, **kwargs: object) -> FakeThread:
        self.resume_calls.append({"thread_id": thread_id, **kwargs})
        self.thread.id = thread_id
        return self.thread


class FakeFactory:
    def __init__(self, client: FakeAsyncCodex) -> None:
        self.client = client
        self.configs: list[object] = []

    def __call__(self, config: object) -> FakeAsyncCodex:
        self.configs.append(config)
        return self.client


class TrackingStream:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_existing_session_validates_account_and_context_lifecycle() -> None:
    fake = FakeAsyncCodex()
    client = CodexSdkClient(AuthMode.EXISTING_SESSION, factory=FakeFactory(fake))

    models = await client.available_models()

    assert models == frozenset({"example-model", "other-model"})
    assert fake.account_calls == 1
    assert fake.login_calls == []
    assert (fake.enters, fake.exits) == (1, 1)


@pytest.mark.asyncio
async def test_api_key_mode_requires_key_and_logs_in_exactly_once() -> None:
    missing = FakeAsyncCodex()
    with pytest.raises(AuthenticationError, match="OPENAI_API_KEY"):
        await CodexSdkClient(
            AuthMode.API_KEY, api_key=None, environ={}, factory=FakeFactory(missing)
        ).available_models()
    assert missing.account_calls == 0
    assert missing.login_calls == []

    fake = FakeAsyncCodex()
    client = CodexSdkClient(
        AuthMode.API_KEY,
        api_key="sk-test-only",
        environ={},
        factory=FakeFactory(fake),
    )
    await client.available_models()
    assert fake.login_calls == ["sk-test-only"]
    assert fake.account_calls == 0


@pytest.mark.asyncio
async def test_read_only_session_files_are_staged_into_writable_codex_home(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    staging = tmp_path / "staging"
    source.mkdir()
    for name in ("auth.json", "config.toml", "models_cache.json"):
        (source / name).write_text(f"safe-{name}\n", encoding="utf-8")
    (source / "unrelated.sqlite").write_text("do not copy\n", encoding="utf-8")
    fake = FakeAsyncCodex()
    factory = FakeFactory(fake)
    client = CodexSdkClient(
        AuthMode.EXISTING_SESSION,
        factory=factory,
        codex_home_source=source,
        codex_home_staging=staging,
    )

    await client.available_models()

    assert sorted(path.name for path in staging.iterdir()) == [
        "auth.json",
        "config.toml",
        "models_cache.json",
    ]
    assert (source / "unrelated.sqlite").read_text(encoding="utf-8") == "do not copy\n"
    assert factory.configs[0].env == {"CODEX_HOME": str(staging)}


@pytest.mark.asyncio
async def test_sdk_transport_output_streams_are_closed_after_context_exit() -> None:
    fake = FakeAsyncCodex()
    stdout = TrackingStream()
    stderr = TrackingStream()
    process = SimpleNamespace(stdout=stdout, stderr=stderr)
    fake._client = SimpleNamespace(_sync=SimpleNamespace(_proc=process))
    client = CodexSdkClient(AuthMode.EXISTING_SESSION, factory=FakeFactory(fake))

    await client.available_models()

    assert stdout.closed
    assert stderr.closed


@pytest.mark.asyncio
async def test_new_thread_maps_route_and_normalizes_result(tmp_path: Path) -> None:
    fake = FakeAsyncCodex()
    client = CodexSdkClient(AuthMode.EXISTING_SESSION, factory=FakeFactory(fake))
    schema = {"type": "object"}

    result = await client.run_new_thread(
        project_path=tmp_path,
        route=_route(),
        prompt="Inspect the project",
        developer_instructions="Follow repository instructions.",
        output_schema=schema,
    )

    start = fake.start_calls[0]
    assert start["cwd"] == str(tmp_path)
    assert start["model"] == "example-model"
    assert start["sandbox"].value == "read-only"
    assert start["developer_instructions"] == "Follow repository instructions."
    run = fake.thread.run_calls[0]
    assert run["prompt"] == "Inspect the project"
    assert run["effort"].value == "low"
    assert run["output_schema"] == schema
    assert result.thread_id == "thread-new"
    assert result.turn_id == "turn-1"
    assert result.status is WorkStatus.SUCCEEDED
    assert result.final_response == "done"
    assert result.sdk_items == ({"type": "message", "text": "safe"},)
    assert (result.input_tokens, result.cached_input_tokens, result.output_tokens) == (
        100,
        25,
        50,
    )
    assert result.duration_seconds == 1.5


@pytest.mark.asyncio
async def test_resume_uses_stored_thread_id(tmp_path: Path) -> None:
    fake = FakeAsyncCodex()
    client = CodexSdkClient(AuthMode.EXISTING_SESSION, factory=FakeFactory(fake))

    result = await client.resume_thread(
        thread_id="stored-thread",
        project_path=tmp_path,
        route=replace(_route(), sandbox=SandboxMode.WORKSPACE_WRITE),
        prompt="Continue",
    )

    assert fake.resume_calls[0]["thread_id"] == "stored-thread"
    assert fake.resume_calls[0]["sandbox"].value == "workspace-write"
    assert result.thread_id == "stored-thread"


@pytest.mark.asyncio
async def test_timeout_and_missing_final_response_map_to_explicit_errors(
    tmp_path: Path,
) -> None:
    slow = FakeAsyncCodex(delay=0.05)
    slow_client = CodexSdkClient(AuthMode.EXISTING_SESSION, factory=FakeFactory(slow))
    with pytest.raises(TurnTimeoutError):
        await slow_client.run_new_thread(
            project_path=tmp_path,
            route=replace(_route(), timeout_seconds=0.001),  # type: ignore[arg-type]
            prompt="slow",
            developer_instructions="instructions",
        )
    assert slow.exits == 1

    missing = FakeAsyncCodex(turn=_turn(final_response=None))
    missing_client = CodexSdkClient(
        AuthMode.EXISTING_SESSION, factory=FakeFactory(missing)
    )
    with pytest.raises(SdkError, match="final response"):
        await missing_client.run_new_thread(
            project_path=tmp_path,
            route=_route(),
            prompt="missing",
            developer_instructions="instructions",
        )
