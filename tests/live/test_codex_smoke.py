# SPDX-License-Identifier: MIT
# File: tests/live/test_codex_smoke.py
# Purpose: Opt-in live AsyncCodex read-only smoke verification

import os
from pathlib import Path

import pytest

from goalrouter.config import load_router_config
from goalrouter.domain import AccessMode, AuthMode, WorkItem
from goalrouter.routing import TaskRouter
from goalrouter.sdk.codex import CodexSdkClient


@pytest.mark.skipif(
    os.environ.get("GOALROUTER_LIVE_TEST") != "1",
    reason="set GOALROUTER_LIVE_TEST=1 for the billable SDK smoke",
)
@pytest.mark.asyncio
async def test_live_read_only_turn(tmp_path: Path) -> None:
    config_path = Path(os.environ["GOALROUTER_CONFIG"])
    config = load_router_config(config_path)
    router = TaskRouter(config)
    item = WorkItem(
        id="live-smoke",
        title="Summarize fixture",
        instructions="Return one sentence describing the files in this project.",
        task=config.default_task,
        phase="verification",
        dependencies=(),
        access=AccessMode.READ_ONLY,
        affected_paths=(),
        expected_result="One sentence.",
        verification=(),
        confidence=1.0,
        risk_flags=frozenset(),
    )
    route = router.route(item)
    (tmp_path / "README.md").write_text("# Live smoke fixture\n", encoding="utf-8")
    client = CodexSdkClient(AuthMode(os.environ.get("GOALROUTER_AUTH_MODE", "existing-session")))
    router.validate_models(await client.available_models())

    result = await client.run_new_thread(
        project_path=tmp_path,
        route=route,
        prompt=item.instructions,
        developer_instructions="Read only. Do not modify files.",
    )

    assert result.final_response
    assert result.input_tokens + result.output_tokens > 0


@pytest.mark.skipif(
    os.environ.get("GOALROUTER_LIVE_INVENTORY") != "1",
    reason="set GOALROUTER_LIVE_INVENTORY=1 for non-billable model inventory",
)
@pytest.mark.asyncio
async def test_live_existing_session_model_inventory_without_agent_turn() -> None:
    assert os.environ.get("GOALROUTER_AUTH_MODE", "existing-session") == "existing-session"
    assert "OPENAI_API_KEY" not in os.environ
    config = load_router_config(Path(os.environ["GOALROUTER_CONFIG"]))
    client = CodexSdkClient(AuthMode.EXISTING_SESSION)

    models = await client.available_models()

    TaskRouter(config).validate_models(models)
    assert models
