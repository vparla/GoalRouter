# SPDX-License-Identifier: MIT
# File: tests/distribution/test_launcher_integration.py
# Purpose: End-to-end launcher/runtime protocol and failure contracts

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Final, Self

import pytest

from goalrouter.cli import build_parser
from goalrouter.domain import AuthMode
from goalrouter.errors import AuthenticationError
from goalrouter.sdk.codex import CodexSdkClient

LAUNCHER: Final = Path("scripts/goalrouter").resolve()
DIGEST: Final = "sha256:" + ("a" * 64)
EXPECTED_PROTOCOL_ERROR: Final = {
    "status": "error",
    "code": "launcher_protocol_mismatch",
    "message": "Launcher protocol 1 cannot run image protocol 2.",
}
APPLICATION_INVOCATIONS: Final = (
    ("config", ("config", "template")),
    ("version", ("version",)),
    ("models", ("models",)),
    ("route", ("route", "--task", "documentation", "--prompt", "Explain it")),
    ("plan", ("plan", "--objective", "Plan it")),
    ("run", ("run", "--objective", "Run it")),
    ("status", ("status", "run-1")),
    (
        "approve",
        ("approve", "run-1", "work-1", "--approved-by", "reviewer"),
    ),
    ("resume", ("resume", "run-1")),
    ("report", ("report", "run-1")),
)


def _launcher_fixture(root: Path) -> tuple[Path, dict[str, str], Path]:
    home = root / "home"
    project = root / "project"
    config = home / ".config" / "goalrouter" / "task-models.yaml"
    state = home / ".local" / "state" / "goalrouter"
    codex_home = home / ".codex"
    fake_bin = root / "bin"
    calls = root / "docker.calls"
    for directory in (project, config.parent, state, codex_home, fake_bin):
        directory.mkdir(parents=True, exist_ok=True)
    config.write_text("schema-version: 1\n", encoding="utf-8")
    (state / "image-ref").write_text("example/goalrouter", encoding="ascii")
    (state / "image-digest").write_text(DIGEST, encoding="ascii")
    (codex_home / "auth.json").write_text("{}\n", encoding="ascii")
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        ": \"${GOALROUTER_TEST_DOCKER_CALLS:?}\"\n"
        "printf '%s\\0' \"$@\" >>\"$GOALROUTER_TEST_DOCKER_CALLS\"\n"
        "printf '\\n' >>\"$GOALROUTER_TEST_DOCKER_CALLS\"\n"
        "case \" $* \" in\n"
        "  *' --json version '*)\n"
        "    if [ \"${GOALROUTER_TEST_DOCKER_FAILURE_PHASE:-}\" = preflight ]; then\n"
        "      printf '%s\\n' \"${GOALROUTER_TEST_DOCKER_ERROR:?}\" >&2\n"
        "      exit 42\n"
        "    fi\n"
        "    printf '{\"version\":\"2.0.0\",\"protocol_version\":%s}\\n' "
        "\"${GOALROUTER_TEST_PROTOCOL:-2}\" ;;\n"
        "  *)\n"
        "    if [ -n \"${GOALROUTER_TEST_STATE_MUTATION:-}\" ]; then\n"
        "      case \" $* \" in\n"
        "        *'dst=/state'*) printf '%s\\n' mutated >\"$GOALROUTER_TEST_STATE_MUTATION\" ;;\n"
        "      esac\n"
        "    fi\n"
        "    if [ \"${GOALROUTER_TEST_DOCKER_FAILURE_PHASE:-}\" = runtime ]; then\n"
        "      printf '%s\\n' \"${GOALROUTER_TEST_DOCKER_ERROR:?}\" >&2\n"
        "      exit 42\n"
        "    fi\n"
        "    printf '%s\\n' 'SDK_INITIALIZATION_MUST_NOT_RUN' ;;\n"
        "esac\n",
        encoding="ascii",
    )
    docker.chmod(0o555)
    environment = {
        "GOALROUTER_TEST_DOCKER_CALLS": str(calls),
        "HOME": str(home),
        "PATH": f"{fake_bin}:/usr/local/bin:/usr/bin:/bin",
    }
    return project, environment, calls


def test_protocol_regression_cases_cover_every_python_application_command() -> None:
    command_action = next(
        action for action in build_parser()._actions if action.dest == "command"
    )

    assert set(command_action.choices or {}) == {
        command for command, _arguments in APPLICATION_INVOCATIONS
    }


@pytest.mark.parametrize(
    ("command", "arguments"),
    APPLICATION_INVOCATIONS,
    ids=[command for command, _arguments in APPLICATION_INVOCATIONS],
)
def test_protocol_major_mismatch_fails_before_every_application_invocation(
    tmp_path: Path,
    command: str,
    arguments: tuple[str, ...],
) -> None:
    project, environment, calls = _launcher_fixture(tmp_path)
    mutation_marker = tmp_path / f"{command}.state-mutated"
    environment["GOALROUTER_TEST_STATE_MUTATION"] = str(mutation_marker)

    result = subprocess.run(
        [str(LAUNCHER), "--json", *arguments],
        cwd=project,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert json.loads(result.stdout) == EXPECTED_PROTOCOL_ERROR
    assert result.stderr == ""
    assert "SDK_INITIALIZATION_MUST_NOT_RUN" not in result.stdout
    assert not mutation_marker.exists()
    raw_calls = calls.read_bytes()
    assert raw_calls.count(b"\n") == 1
    assert b"--json\x00version\x00" in raw_calls
    assert b"/var/run/docker.sock" not in raw_calls
    assert b"/project" not in raw_calls
    assert b"/state" not in raw_calls


def test_launcher_failure_categories_are_cross_platform_and_secret_free() -> None:
    expected = {
        "prerequisite",
        "configuration",
        "authentication",
        "registry",
        "mount",
        "permission",
        "application",
        "launcher_protocol_mismatch",
    }
    posix = LAUNCHER.read_text(encoding="utf-8")
    powershell = Path("scripts/goalrouter.ps1").read_text(encoding="utf-8")

    for category in expected:
        assert category in posix
        assert category in powershell
    assert "OPENAI_API_KEY=" not in posix
    assert "OPENAI_API_KEY=" not in powershell


@pytest.mark.parametrize(
    ("category", "native_error", "phase"),
    [
        ("prerequisite", "Cannot connect to the Docker daemon task8-secret", "runtime"),
        ("configuration", "configuration schema rejected task8-secret", "runtime"),
        ("authentication", "Codex authentication expired task8-secret", "runtime"),
        ("registry", "registry manifest unknown for image task8-secret", "preflight"),
        ("mount", "invalid mount specification task8-secret", "runtime"),
        ("permission", "permission denied by runtime task8-secret", "runtime"),
        ("application", "agent execution failed task8-secret", "runtime"),
    ],
)
def test_posix_native_failures_are_categorized_and_redacted(
    tmp_path: Path,
    category: str,
    native_error: str,
    phase: str,
) -> None:
    project, environment, _calls = _launcher_fixture(tmp_path)
    environment.update(
        {
            "GOALROUTER_TEST_DOCKER_ERROR": native_error,
            "GOALROUTER_TEST_DOCKER_FAILURE_PHASE": phase,
            "GOALROUTER_TEST_PROTOCOL": "1",
        }
    )

    result = subprocess.run(
        [str(LAUNCHER), "--project", str(project), "--json", "models"],
        cwd=project,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload == {
        "status": "error",
        "code": category,
        "message": f"GoalRouter launcher failed in the {category} category.",
    }
    assert result.stderr == ""
    assert "task8-secret" not in result.stdout


def test_missing_api_key_is_authentication_json_without_docker_fallback(
    tmp_path: Path,
) -> None:
    project, environment, calls = _launcher_fixture(tmp_path)

    result = subprocess.run(
        [str(LAUNCHER), "--json", "--auth-mode", "api-key", "models"],
        cwd=project,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert json.loads(result.stdout)["code"] == "authentication"
    assert result.stderr == ""
    assert not calls.exists()


def test_missing_existing_session_never_falls_back_to_available_api_key(
    tmp_path: Path,
) -> None:
    project, environment, calls = _launcher_fixture(tmp_path)
    codex_home = Path(environment["HOME"]) / ".codex"
    (codex_home / "auth.json").unlink()
    codex_home.rmdir()
    secret = "task8-api-key-must-not-appear"
    environment["OPENAI_API_KEY"] = secret

    result = subprocess.run(
        [str(LAUNCHER), "--json", "models"],
        cwd=project,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert json.loads(result.stdout)["code"] == "authentication"
    assert result.stderr == ""
    assert secret not in result.stdout
    assert secret not in result.stderr
    assert not calls.exists()


class _RejectedSession:
    def __init__(self, staged_auth: Path) -> None:
        self.staged_auth = staged_auth
        self.login_calls: list[str] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def account(self) -> object:
        session = json.loads(self.staged_auth.read_text(encoding="utf-8"))
        if session.get("expired") or session.get("unauthorized"):
            raise RuntimeError("expired-or-unauthorized")
        return SimpleNamespace(account=None)

    async def login_api_key(self, key: str) -> None:
        self.login_calls.append(key)

    async def models(self) -> object:
        raise AssertionError("models must not run after rejected authentication")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth_contents",
    ["not-json", '{"expired":true}', '{"unauthorized":true}'],
)
async def test_corrupt_expired_or_unauthorized_session_has_no_api_key_fallback(
    tmp_path: Path,
    auth_contents: str,
) -> None:
    source = tmp_path / "source"
    staging = tmp_path / "staging"
    source.mkdir()
    (source / "auth.json").write_text(auth_contents, encoding="utf-8")
    rejected = _RejectedSession(staging / "auth.json")
    client = CodexSdkClient(
        AuthMode.EXISTING_SESSION,
        environ={"OPENAI_API_KEY": "task8-key-must-not-fallback"},
        codex_home_source=source,
        codex_home_staging=staging,
        factory=lambda config: rejected,  # type: ignore[arg-type,return-value]
    )

    with pytest.raises(AuthenticationError):
        await client.available_models()

    assert rejected.login_calls == []
    assert (staging / "auth.json").read_text(encoding="utf-8") == auth_contents


@pytest.mark.asyncio
async def test_unreadable_session_is_authentication_and_never_reaches_sdk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    staging = tmp_path / "staging"
    source.mkdir()
    auth = source / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    factory_calls: list[object] = []

    def reject_auth_copy(source_path: Path, destination: Path) -> None:
        del destination
        if source_path == auth:
            raise PermissionError("unreadable auth fixture")
        raise AssertionError(f"unexpected staged input: {source_path}")

    monkeypatch.setattr("goalrouter.sdk.codex.shutil.copy2", reject_auth_copy)
    client = CodexSdkClient(
        AuthMode.EXISTING_SESSION,
        environ={"OPENAI_API_KEY": "task8-key-must-not-fallback"},
        codex_home_source=source,
        codex_home_staging=staging,
        factory=lambda config: factory_calls.append(config),  # type: ignore[arg-type,return-value]
    )

    with pytest.raises(AuthenticationError):
        await client.available_models()

    assert factory_calls == []
