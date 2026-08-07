# SPDX-License-Identifier: MIT
# File: src/goalrouter/sdk/codex.py
# Purpose: Concrete async adapter for the OpenAI Codex SDK

"""Translate GoalRouter domain requests to the OpenAI Codex SDK."""

import asyncio
import os
import shutil
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Protocol, cast

from openai_codex import (
    ApprovalMode as SdkApprovalMode,
    AsyncCodex,
    CodexConfig,
    Sandbox,
    TurnResult,
)
from openai_codex.generated.v2_all import ReasoningEffort, ThreadItem
from openai_codex.models import JsonObject

from goalrouter.domain import (
    AuthMode,
    JsonValue,
    RouteDecision,
    SandboxMode,
    WorkResult,
    WorkStatus,
)
from goalrouter.errors import AuthenticationError, SdkError, TurnTimeoutError

type CodexFactory = Callable[[CodexConfig], AsyncCodex]

_SESSION_FILES = ("auth.json", "config.toml", "models_cache.json")


class _ClosableStream(Protocol):
    def close(self) -> None: ...


class _TransportProcess(Protocol):
    stdout: _ClosableStream | None
    stderr: _ClosableStream | None


class _ProcessOwner(Protocol):
    _proc: _TransportProcess | None


class _SyncOwner(Protocol):
    _sync: _ProcessOwner


class _ClientOwner(Protocol):
    _client: _SyncOwner


class CodexSdkClient:
    """Run bounded Codex turns using one explicitly selected auth mode."""

    def __init__(
        self,
        auth_mode: AuthMode,
        *,
        api_key: str | None = None,
        environ: Mapping[str, str] | None = None,
        codex_bin: str | None = None,
        codex_home_source: Path | None = None,
        codex_home_staging: Path | None = None,
        operation_timeout_seconds: float = 30.0,
        factory: CodexFactory = AsyncCodex,
    ) -> None:
        self._auth_mode = auth_mode
        self._api_key = api_key
        self._environ = os.environ if environ is None else environ
        source_value = self._environ.get("GOALROUTER_CODEX_HOME")
        staging_value = self._environ.get("GOALROUTER_CODEX_STAGING_PATH")
        self._codex_home_source = codex_home_source or (
            Path(source_value) if source_value else None
        )
        self._codex_home_staging = codex_home_staging or (
            Path(staging_value) if staging_value else None
        )
        if (self._codex_home_source is None) is not (
            self._codex_home_staging is None
        ):
            raise SdkError(
                "Codex home staging requires both source and staging paths"
            )
        self._staging_lock = asyncio.Lock()
        self._staged = self._codex_home_source is None
        self._operation_timeout_seconds = operation_timeout_seconds
        self._factory = factory
        sdk_environment = (
            {"CODEX_HOME": str(self._codex_home_staging)}
            if self._codex_home_staging is not None
            else None
        )
        self._config = CodexConfig(codex_bin=codex_bin, env=sdk_environment)

    async def available_models(self) -> frozenset[str]:
        """Return concrete model identifiers available to the selected account."""

        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                await self._prepare_codex_home()
                async with _managed_codex(self._factory, self._config) as codex:
                    await self._authenticate(codex)
                    response = await codex.models()
                    return frozenset(model.id for model in response.data)
        except TimeoutError as error:
            raise SdkError("Timed out while listing available Codex models") from error
        except (AuthenticationError, SdkError):
            raise
        except Exception as error:
            raise SdkError("Codex SDK could not list available models") from error

    async def run_new_thread(
        self,
        *,
        project_path: Path,
        route: RouteDecision,
        prompt: str,
        developer_instructions: str,
        output_schema: Mapping[str, object] | None = None,
    ) -> WorkResult:
        """Start a new Codex thread and normalize its completed turn."""

        try:
            async with asyncio.timeout(route.timeout_seconds):
                await self._prepare_codex_home()
                async with _managed_codex(self._factory, self._config) as codex:
                    await self._authenticate(codex)
                    thread = await codex.thread_start(
                        approval_mode=SdkApprovalMode.deny_all,
                        cwd=str(project_path),
                        developer_instructions=developer_instructions,
                        model=route.model,
                        sandbox=_sandbox(route.sandbox),
                    )
                    turn = await thread.run(
                        prompt,
                        effort=ReasoningEffort(route.reasoning_effort),
                        output_schema=_output_schema(output_schema),
                    )
                    return _normalize_result(route, thread.id, turn)
        except TimeoutError as error:
            raise TurnTimeoutError(
                f"Codex turn exceeded the {route.timeout_seconds}-second timeout"
            ) from error
        except (AuthenticationError, SdkError):
            raise
        except Exception as error:
            raise SdkError("Codex SDK could not complete a new thread") from error

    async def resume_thread(
        self,
        *,
        thread_id: str,
        project_path: Path,
        route: RouteDecision,
        prompt: str,
        output_schema: Mapping[str, object] | None = None,
    ) -> WorkResult:
        """Resume a persisted Codex thread and normalize its completed turn."""

        try:
            async with asyncio.timeout(route.timeout_seconds):
                await self._prepare_codex_home()
                async with _managed_codex(self._factory, self._config) as codex:
                    await self._authenticate(codex)
                    thread = await codex.thread_resume(
                        thread_id,
                        approval_mode=SdkApprovalMode.deny_all,
                        cwd=str(project_path),
                        model=route.model,
                        sandbox=_sandbox(route.sandbox),
                    )
                    turn = await thread.run(
                        prompt,
                        effort=ReasoningEffort(route.reasoning_effort),
                        output_schema=_output_schema(output_schema),
                    )
                    return _normalize_result(route, thread.id, turn)
        except TimeoutError as error:
            raise TurnTimeoutError(
                f"Codex turn exceeded the {route.timeout_seconds}-second timeout"
            ) from error
        except (AuthenticationError, SdkError):
            raise
        except Exception as error:
            raise SdkError("Codex SDK could not resume the stored thread") from error

    async def _authenticate(self, codex: AsyncCodex) -> None:
        try:
            if self._auth_mode is AuthMode.EXISTING_SESSION:
                response = await codex.account()
                if response.account is None:
                    raise AuthenticationError(
                        "No authenticated Codex session is available; log in with Codex first"
                    )
                return

            api_key = self._api_key or self._environ.get("OPENAI_API_KEY")
            if not api_key:
                raise AuthenticationError(
                    "API-key authentication requires OPENAI_API_KEY or an explicit API key"
                )
            await codex.login_api_key(api_key)
        except AuthenticationError:
            raise
        except Exception as error:
            raise AuthenticationError(
                f"The selected {self._auth_mode.value} authentication mode failed"
            ) from error

    async def _prepare_codex_home(self) -> None:
        if self._staged:
            return
        async with self._staging_lock:
            await self._stage_codex_home_once()

    async def _stage_codex_home_once(self) -> None:
        if self._staged:
            return
        source = self._codex_home_source
        staging = self._codex_home_staging
        if source is None or staging is None:
            raise SdkError("Codex home staging paths are incomplete")
        try:
            await asyncio.to_thread(_stage_codex_home, source, staging)
        except OSError as error:
            raise AuthenticationError(
                "Cannot stage the selected Codex authentication session"
            ) from error
        self._staged = True


def _sandbox(mode: SandboxMode) -> Sandbox:
    if mode is SandboxMode.READ_ONLY:
        return Sandbox.read_only
    if mode is SandboxMode.WORKSPACE_WRITE:
        return Sandbox.workspace_write
    raise SdkError(f"Unsupported sandbox mode: {mode}")


def _output_schema(schema: Mapping[str, object] | None) -> JsonObject | None:
    if schema is None:
        return None
    return cast("JsonObject", dict(schema))


def _normalize_result(
    route: RouteDecision,
    thread_id: str,
    turn: TurnResult,
) -> WorkResult:
    status = turn.status.value
    if status != "completed":
        raise SdkError(f"Codex turn ended with status {status}")
    if turn.final_response is None:
        raise SdkError("Codex turn completed without a final response")

    usage = turn.usage.total if turn.usage is not None else None
    duration_seconds = (turn.duration_ms or 0) / 1000
    return WorkResult(
        work_item_id=route.task,
        thread_id=thread_id,
        turn_id=turn.id,
        status=WorkStatus.SUCCEEDED,
        final_response=turn.final_response,
        sdk_items=tuple(_sdk_item(item) for item in turn.items),
        input_tokens=usage.input_tokens if usage is not None else 0,
        cached_input_tokens=usage.cached_input_tokens if usage is not None else 0,
        output_tokens=usage.output_tokens if usage is not None else 0,
        duration_seconds=duration_seconds,
        changed_paths=(),
        verification=(),
        confidence=1.0,
        escalation_requested=False,
        error=None,
    )


def _sdk_item(item: object) -> JsonValue:
    if isinstance(item, ThreadItem):
        return cast("JsonValue", item.model_dump(mode="json", by_alias=True))
    if isinstance(item, Mapping) and all(isinstance(key, str) for key in item):
        return {str(key): _json_value(value) for key, value in item.items()}
    return {"type": type(item).__name__}


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping) and all(isinstance(key, str) for key in value):
        return {str(key): _json_value(item) for key, item in value.items()}
    return str(value)


def _stage_codex_home(source: Path, staging: Path) -> None:
    resolved_source = source.resolve(strict=True)
    if not resolved_source.is_dir():
        raise NotADirectoryError(resolved_source)
    resolved_staging = staging.resolve(strict=False)
    if resolved_staging == resolved_source:
        raise OSError("Codex staging path must differ from the read-only source")
    resolved_staging.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved_staging.chmod(0o700)
    for name in _SESSION_FILES:
        candidate = resolved_source / name
        if candidate.is_file():
            destination = resolved_staging / name
            shutil.copy2(candidate, destination)
            destination.chmod(0o600)


@asynccontextmanager
async def _managed_codex(
    factory: CodexFactory,
    config: CodexConfig,
) -> AsyncIterator[AsyncCodex]:
    instance = factory(config)
    process: _TransportProcess | None = None
    try:
        async with instance as codex:
            try:
                yield codex
            finally:
                process = _transport_process(codex)
    finally:
        if process is not None:
            await asyncio.to_thread(_close_output_streams, process)


def _transport_process(codex: object) -> _TransportProcess | None:
    try:
        return cast("_ClientOwner", codex)._client._sync._proc
    except AttributeError:
        return None


def _close_output_streams(process: _TransportProcess) -> None:
    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()
