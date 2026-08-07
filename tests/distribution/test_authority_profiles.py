# SPDX-License-Identifier: MIT
# File: tests/distribution/test_authority_profiles.py
# Purpose: Verify exact launcher and live-Compose authority profiles

import io
import json
import os
import selectors
import subprocess
import textwrap
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO, Self

import pytest
import yaml


def test_live_profiles_have_exact_project_and_socket_authority() -> None:
    document = yaml.safe_load(Path("compose.live.yaml").read_text(encoding="utf-8"))
    services = document["services"]
    expected = {
        "cli-readonly": ("/project:ro", False),
        "cli-write": ("/project:rw", False),
        "cli-docker": ("/project:rw", True),
    }

    for name, (project_suffix, has_socket) in expected.items():
        service = services[name]
        volumes = service["volumes"]
        assert any(value.endswith(project_suffix) for value in volumes)
        assert (
            "/var/run/docker.sock:/var/run/docker.sock:rw" in volumes
        ) is has_socket
        assert service.get("privileged") is not True
        assert service.get("pid") != "host"
        assert service.get("network_mode") != "host"
        assert service.get("devices", []) == []
        assert all(":/root" not in value and ":/home" not in value for value in volumes)


def test_existing_session_live_boundary_does_not_forward_api_key() -> None:
    document = yaml.safe_load(Path("compose.live.yaml").read_text(encoding="utf-8"))
    common = document["x-cli-common"]
    live_test = document["services"]["live-test"]

    assert "OPENAI_API_KEY" not in common["environment"]
    assert "OPENAI_API_KEY" not in live_test["environment"]


def test_live_inventory_uses_generated_installed_launcher_boundary() -> None:
    document = yaml.safe_load(Path("compose.live.yaml").read_text(encoding="utf-8"))
    service = document["services"]["live-inventory"]

    assert service["build"]["target"] == "posix-installer-smoke"
    assert service["entrypoint"] == [
        "/bin/sh",
        "/workspace/scripts/posix-installer-smoke.sh",
    ]
    assert service["environment"]["GOALROUTER_INSTALLED_LIVE_INVENTORY"] == "1"
    assert "OPENAI_API_KEY" not in service["environment"]
    harness = Path("scripts/posix-installer-smoke.sh").read_text(encoding="utf-8")
    assert '"$HOME/.local/bin/goalrouter"' in harness
    assert "--auth-mode existing-session" in harness
    assert "--access readonly" in harness
    assert "--json models" in harness


def test_distribution_integration_service_is_explicit_and_least_authority() -> None:
    document = yaml.safe_load(Path("compose.yaml").read_text(encoding="utf-8"))
    service = document["services"]["distribution-integration"]

    assert service["network_mode"] == "none"
    assert service["read_only"] is True
    assert service.get("privileged") is not True
    assert service.get("pid") != "host"
    assert service.get("devices", []) == []
    assert service["environment"] == {
        "DOCKER_BUILDKIT": "1",
        "GOALROUTER_DISTRIBUTION_INTEGRATION": "1",
        "HOME": "/tmp/docker-home",
    }
    assert service["volumes"] == [
        "/var/run/docker.sock:/var/run/docker.sock:rw",
    ]


def test_real_authority_harness_uses_repo_digest_and_state_only_volume() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    harness = source.split(
        "def test_real_launcher_profiles_enforce_mounts_socket_and_numeric_ownership",
        maxsplit=1,
    )[1]

    assert 'state_volume = f"goalrouter-task8-state-' in harness
    assert "state_mountpoint" in harness
    assert 'f"{state_volume}:/state:rw"' in harness
    assert "probe_repo_digest" in harness
    assert '"--image",\n                probe_repo_digest,' in harness


def _docker(
    *arguments: str,
    check: bool = True,
    timeout_seconds: float | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if check and result.returncode != 0:
        pytest.fail(
            f"docker {' '.join(arguments)} exited {result.returncode}: {result.stderr}"
        )
    return result


def test_registry_publication_waits_for_listening_event_before_push() -> None:
    events: list[str] = []
    ready = False

    def runner(
        *arguments: str,
        check: bool = True,
        timeout_seconds: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del check, timeout_seconds
        if arguments[:2] == ("image", "push"):
            assert ready
        events.append(" ".join(arguments))
        return subprocess.CompletedProcess(
            ["docker", *arguments],
            returncode=0,
            stdout="",
            stderr="",
        )

    def wait_for_ready(container_name: str) -> None:
        nonlocal ready
        assert container_name == "owned-registry"
        events.append("registry listening")
        ready = True

    _start_registry_and_wait(
        "owned-registry",
        runner=runner,
        readiness_waiter=wait_for_ready,
    )
    runner("image", "push", "registry:base")

    assert events == [
        "container start owned-registry",
        "registry listening",
        "image push registry:base",
    ]


def _start_registry_and_wait(
    registry_name: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    readiness_waiter: Callable[[str], None],
) -> None:
    runner("container", "start", registry_name)
    readiness_waiter(registry_name)


def test_registry_log_boundary_reaps_follower_after_listening_event() -> None:
    class LogFollower:
        def __init__(self) -> None:
            self.stdout = io.BytesIO(
                b'time="2026-08-06T00:00:00Z" level=debug msg="starting"\n'
                b'time="2026-08-06T00:00:00Z" level=info msg="listening on [::]:5000"\n'
            )
            self.terminated = False
            self.reaped = False

        def poll(self) -> int | None:
            return None if not self.terminated else -15

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            raise AssertionError("the responsive follower must not be killed")

        def communicate(self, timeout: float) -> tuple[bytes, bytes]:
            assert timeout > 0
            self.reaped = True
            return (self.stdout.read(), b"")

    class ReadySelector:
        def __init__(self) -> None:
            self.select_count = 0

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_arguments: object) -> None:
            return None

        def register(self, stream: io.BytesIO, events: int) -> None:
            assert stream is follower.stdout
            assert events == selectors.EVENT_READ

        def select(self, timeout: float) -> list[tuple[object, int]]:
            assert timeout > 0
            self.select_count += 1
            if self.select_count == 1:
                return [(object(), selectors.EVENT_READ)]
            return []

    follower = LogFollower()
    commands: list[list[str]] = []

    def process_factory(command: list[str], **options: object) -> LogFollower:
        commands.append(command)
        assert options == {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
        }
        return follower

    _wait_for_registry_ready(
        "owned-registry",
        timeout_seconds=5,
        process_factory=process_factory,
        selector_factory=ReadySelector,
        chunk_reader=lambda stream: stream.read(),
    )

    assert commands == [
        ["docker", "container", "logs", "--follow", "owned-registry"]
    ]
    assert follower.terminated is True
    assert follower.reaped is True


def _wait_for_registry_ready(
    container_name: str,
    *,
    timeout_seconds: float,
    process_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    selector_factory: Callable[[], selectors.BaseSelector] = selectors.DefaultSelector,
    chunk_reader: Callable[[BinaryIO], bytes] = lambda stream: os.read(
        stream.fileno(), 4096
    ),
) -> None:
    process = process_factory(
        ["docker", "container", "logs", "--follow", container_name],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if process.stdout is None:
        process.kill()
        process.communicate(timeout=2)
        raise AssertionError("registry log follower did not expose stdout")

    captured_logs = bytearray()
    ready = False
    failure_reason = ""
    deadline = time.monotonic() + timeout_seconds
    try:
        with selector_factory() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while not ready:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure_reason = "timed out"
                    break
                if not selector.select(timeout=remaining):
                    failure_reason = "timed out"
                    break
                chunk = chunk_reader(process.stdout)
                if chunk == b"":
                    failure_reason = (
                        f"log follower exited with status {process.poll()}"
                    )
                    break
                captured_logs.extend(chunk)
                ready = b"listening on [::]:5000" in captured_logs
    finally:
        if process.poll() is None:
            with suppress(ProcessLookupError):
                process.terminate()
        try:
            remaining_stdout, _ = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                process.kill()
            remaining_stdout, _ = process.communicate(timeout=2)
        if remaining_stdout:
            captured_logs.extend(remaining_stdout)

    if not ready:
        diagnostics = captured_logs.decode(errors="replace").strip() or "<no registry logs>"
        raise AssertionError(
            f"registry {container_name} {failure_reason} before listening; "
            f"captured logs:\n{diagnostics}"
        )


def test_registry_log_boundary_kills_reaps_and_reports_on_timeout() -> None:
    class HungLogFollower:
        def __init__(self) -> None:
            self.stdout = io.BytesIO(b"registry booting\n")
            self.terminated = False
            self.killed = False
            self.reaped = False

        def poll(self) -> int | None:
            return -9 if self.killed else None

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

        def communicate(self, timeout: float) -> tuple[bytes, bytes]:
            if not self.killed:
                raise subprocess.TimeoutExpired("docker logs", timeout)
            self.reaped = True
            return (self.stdout.read(), b"")

    class NeverReadySelector:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_arguments: object) -> None:
            return None

        def register(self, _stream: io.BytesIO, _events: int) -> None:
            return None

        def select(self, timeout: float) -> list[tuple[object, int]]:
            assert timeout > 0
            return []

    follower = HungLogFollower()

    with pytest.raises(AssertionError, match="timed out before listening") as caught:
        _wait_for_registry_ready(
            "owned-registry",
            timeout_seconds=5,
            process_factory=lambda *_args, **_kwargs: follower,
            selector_factory=NeverReadySelector,
        )

    assert "registry booting" in str(caught.value)
    assert follower.terminated is True
    assert follower.killed is True
    assert follower.reaped is True


def test_registry_log_boundary_reaps_without_masking_cancellation() -> None:
    class ExitedLogFollower:
        def __init__(self) -> None:
            self.stdout = io.BytesIO(b"")
            self.communicate_count = 0
            self.reaped = False

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            raise ProcessLookupError

        def kill(self) -> None:
            raise ProcessLookupError

        def communicate(self, timeout: float) -> tuple[bytes, bytes]:
            assert timeout > 0
            self.communicate_count += 1
            if self.communicate_count == 1:
                raise subprocess.TimeoutExpired("docker logs", timeout)
            self.reaped = True
            return (b"", b"")

    class CancellingSelector:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_arguments: object) -> None:
            return None

        def register(self, _stream: io.BytesIO, _events: int) -> None:
            return None

        def select(self, timeout: float) -> list[tuple[object, int]]:
            assert timeout > 0
            raise KeyboardInterrupt

    follower = ExitedLogFollower()

    with pytest.raises(KeyboardInterrupt):
        _wait_for_registry_ready(
            "owned-registry",
            timeout_seconds=5,
            process_factory=lambda *_args, **_kwargs: follower,
            selector_factory=CancellingSelector,
        )

    assert follower.reaped is True


def _attempt_docker_cleanup(
    *arguments: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _docker,
) -> subprocess.CompletedProcess[str] | Exception:
    try:
        return runner(
            *arguments,
            check=False,
            timeout_seconds=10,
        )
    except Exception as error:
        return error


def _remove_owned_volumes(
    volume_names: tuple[str, ...],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _docker,
) -> tuple[subprocess.CompletedProcess[str] | Exception, ...]:
    return tuple(
        _attempt_docker_cleanup("volume", "rm", name, runner=runner)
        for name in volume_names
    )


def _cleanup_failure(
    action: str,
    result: subprocess.CompletedProcess[str] | Exception,
) -> str | None:
    if isinstance(result, Exception):
        return f"{action} raised {type(result).__name__}: {result}"
    if result.returncode != 0:
        return f"{action} exited {result.returncode}: {result.stderr.strip()}"
    return None


def test_owned_volume_cleanup_attempts_every_volume_after_runner_exception() -> None:
    calls: list[tuple[str, ...]] = []

    def runner(
        *arguments: str,
        check: bool = True,
        timeout_seconds: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del check, timeout_seconds
        calls.append(arguments)
        if arguments[-1] == "first-volume":
            raise subprocess.TimeoutExpired(["docker", *arguments], timeout=10)
        return subprocess.CompletedProcess(
            ["docker", *arguments],
            returncode=0,
            stdout="second-volume\n",
            stderr="",
        )

    results = _remove_owned_volumes(
        ("first-volume", "second-volume"),
        runner=runner,
    )

    assert calls == [
        ("volume", "rm", "first-volume"),
        ("volume", "rm", "second-volume"),
    ]
    assert isinstance(results[0], subprocess.TimeoutExpired)
    assert isinstance(results[1], subprocess.CompletedProcess)
    assert results[1].returncode == 0


@pytest.mark.skipif(
    os.environ.get("GOALROUTER_DISTRIBUTION_INTEGRATION") != "1",
    reason="requires the declared Docker-socket integration profile",
)
def test_real_launcher_profiles_enforce_mounts_socket_and_numeric_ownership(
    tmp_path: Path,
) -> None:
    run_id = uuid.uuid4().hex
    owner = "org.goalrouter.test=distribution-integration"
    run_label = f"org.goalrouter.test.run={run_id}"
    base_image = f"goalrouter-task8-base:{run_id}"
    probe_image = f"goalrouter-task8-probe:{run_id}"
    volume = f"goalrouter-task8-authority-{run_id}"
    state_volume = f"goalrouter-task8-state-{run_id}"
    init_name = f"goalrouter-task8-init-{run_id}"
    registry_name = f"goalrouter-task8-registry-{run_id}"
    registry_image = (
        "registry:3.0.0@sha256:"
        "6c5666b861f3505b116bb9aa9b25175e71210414bd010d92035ff64018f9457e"
    )
    registry_base_tag = ""
    registry_probe_tag = ""
    base_repo_digest = ""
    probe_repo_digest = ""
    socket_gid = os.stat("/var/run/docker.sock").st_gid
    context = tmp_path / "probe-image"
    context.mkdir()
    (context / "probe-entrypoint.sh").write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            set -eu
            if [ "${1:-}" = --json ] && [ "${2:-}" = version ]; then
                printf '%s\\n' '{"version":"1.0.0","protocol_version":1}'
                exit 0
            fi
            [ "${1:-}" = authority-probe ]
            mode=${2:?}
            socket=${3:?}
            if touch "/project/requested-$mode" 2>/dev/null; then wrote=yes; else wrote=no; fi
            case $mode in
                readonly) [ "$wrote" = no ] ;;
                write | docker) [ "$wrote" = yes ] ;;
                *) exit 20 ;;
            esac
            if [ "$socket" = present ]; then
                test -S /var/run/docker.sock
            else
                test ! -e /var/run/docker.sock
            fi
            if touch /config/task-models.yaml 2>/dev/null; then exit 21; fi
            if touch /codex-auth/auth.json 2>/dev/null; then exit 22; fi
            printf '%s\\n' "$mode" >"/state/report-$mode"
            """
        ),
        encoding="ascii",
    )
    (context / "Dockerfile").write_text(
        f"FROM {base_image}\n"
        "COPY --chmod=0555 probe-entrypoint.sh /usr/local/bin/task8-probe\n"
        'ENTRYPOINT ["/usr/local/bin/task8-probe"]\n',
        encoding="ascii",
    )

    try:
        _docker(
            "build",
            "--quiet",
            "--pull=false",
            "--target",
            "runtime",
            "--build-arg",
            "VERSION=1.0.0",
            "--build-arg",
            "REVISION=task8-integration",
            "--build-arg",
            "CREATED=2026-08-04T00:00:00Z",
            "--label",
            owner,
            "--label",
            run_label,
            "--tag",
            base_image,
            ".",
        )
        _docker(
            "build",
            "--quiet",
            "--pull=false",
            "--label",
            owner,
            "--label",
            run_label,
            "--tag",
            probe_image,
            str(context),
        )
        image_id = _docker("image", "inspect", "--format", "{{.Id}}", base_image).stdout.strip()
        revision = _docker(
            "image",
            "inspect",
            "--format",
            '{{index .Config.Labels "org.opencontainers.image.revision"}}',
            base_image,
        ).stdout.strip()
        assert image_id.startswith("sha256:")
        assert revision == "task8-integration"

        _docker(
            "container",
            "create",
            "--name",
            registry_name,
            "--label",
            owner,
            "--label",
            run_label,
            "--publish",
            "127.0.0.1::5000",
            registry_image,
        )
        _start_registry_and_wait(
            registry_name,
            runner=_docker,
            readiness_waiter=lambda name: _wait_for_registry_ready(
                name,
                timeout_seconds=10,
            ),
        )
        registry_port = _docker(
            "container", "port", registry_name, "5000/tcp"
        ).stdout.strip().rsplit(":", maxsplit=1)[1]
        registry_base_tag = f"127.0.0.1:{registry_port}/goalrouter-task8-base:{run_id}"
        registry_probe_tag = f"127.0.0.1:{registry_port}/goalrouter-task8-probe:{run_id}"
        for local_image, registry_tag in (
            (base_image, registry_base_tag),
            (probe_image, registry_probe_tag),
        ):
            _docker("image", "tag", local_image, registry_tag)
            _docker("image", "push", registry_tag)
        base_repository = registry_base_tag.rsplit(":", maxsplit=1)[0]
        probe_repository = registry_probe_tag.rsplit(":", maxsplit=1)[0]
        base_repo_digest = next(
            value
            for value in json.loads(
                _docker(
                    "image",
                    "inspect",
                    "--format",
                    "{{json .RepoDigests}}",
                    registry_base_tag,
                ).stdout
            )
            if value.startswith(f"{base_repository}@sha256:")
        )
        probe_repo_digest = next(
            value
            for value in json.loads(
                _docker(
                    "image",
                    "inspect",
                    "--format",
                    "{{json .RepoDigests}}",
                    registry_probe_tag,
                ).stdout
            )
            if value.startswith(f"{probe_repository}@sha256:")
        )

        for owned_volume in (volume, state_volume):
            _docker(
                "volume", "create", "--label", owner, "--label", run_label, owned_volume
            )
        _docker(
            "container",
            "create",
            "--name",
            init_name,
            "--label",
            owner,
            "--label",
            run_label,
            "--volume",
            f"{volume}:/fixture:rw",
            "--volume",
            f"{state_volume}:/state:rw",
            "docker:28.3.3-cli@sha256:0135662b510037ea581d99c2e5929c5e01185139c0b86986a418bd4da0b98a44",
            "/bin/sh",
            "-eu",
            "-c",
            "mkdir -p /fixture/project /fixture/config /fixture/auth; "
            "printf immutable > /fixture/project/immutable.txt; "
            "printf '{}\\n' > /fixture/auth/auth.json; "
            "chown -R 24680:24681 /fixture /state; "
            "chmod 0444 /fixture/auth/auth.json",
        )
        _docker("container", "start", "--attach", init_name)
        _docker("container", "cp", "scripts/goalrouter", f"{init_name}:/fixture/goalrouter")
        _docker(
            "container",
            "cp",
            "config/task-models.yaml",
            f"{init_name}:/fixture/config/task-models.yaml",
        )
        _docker("container", "start", "--attach", init_name)
        _docker("container", "rm", init_name)
        fixture_path = _docker(
            "volume", "inspect", "--format", "{{.Mountpoint}}", volume
        ).stdout.strip()
        state_mountpoint = _docker(
            "volume", "inspect", "--format", "{{.Mountpoint}}", state_volume
        ).stdout.strip()

        for access, socket in (
            ("readonly", "absent"),
            ("write", "absent"),
            ("docker", "present"),
        ):
            result = _docker(
                "run",
                "--rm",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,exec,nosuid,size=64m,mode=1777",
                "--user",
                "24680:24681",
                "--group-add",
                str(socket_gid),
                "--volume",
                "/var/run/docker.sock:/var/run/docker.sock:rw",
                "--volume",
                f"{volume}:{fixture_path}:ro",
                "--volume",
                f"{state_volume}:{state_mountpoint}:rw",
                "--env",
                "HOME=/tmp/launcher-home",
                "docker:28.3.3-cli@sha256:0135662b510037ea581d99c2e5929c5e01185139c0b86986a418bd4da0b98a44",
                f"{fixture_path}/goalrouter",
                "--project",
                f"{fixture_path}/project",
                "--access",
                access,
                "--config",
                f"{fixture_path}/config/task-models.yaml",
                "--state-dir",
                state_mountpoint,
                "--codex-home",
                f"{fixture_path}/auth",
                "--image",
                probe_repo_digest,
                "authority-probe",
                access,
                socket,
                check=False,
            )
            assert result.returncode == 0, result.stderr
            assert result.stdout == ""

        evidence = _docker(
            "run",
            "--rm",
            "--volume",
            f"{volume}:/fixture:ro",
            "--volume",
            f"{state_volume}:/state:ro",
            "--entrypoint",
            "/bin/sh",
            "docker:28.3.3-cli@sha256:0135662b510037ea581d99c2e5929c5e01185139c0b86986a418bd4da0b98a44",
            "-eu",
            "-c",
            "test ! -e /fixture/project/requested-readonly; "
            "test -f /fixture/project/requested-write; "
            "test -f /fixture/project/requested-docker; "
            "test \"$(find /fixture/project -type f | wc -l)\" -eq 3; "
            "stat -c '%u:%g %n' /state/report-* /fixture/project/requested-*",
        ).stdout
        evidence_lines = [line for line in evidence.splitlines() if line]
        assert len(evidence_lines) == 5
        assert all(line.startswith("24680:24681 ") for line in evidence_lines)

        seed = textwrap.dedent(
            """\
            import asyncio
            from datetime import UTC, datetime
            from pathlib import Path

            from goalrouter.config import load_router_config
            from goalrouter.domain import (
                AccessMode,
                Objective,
                RepositoryContext,
                RunState,
                RunStatus,
                WorkItem,
                WorkResult,
                WorkStatus,
            )
            from goalrouter.routing import TaskRouter
            from goalrouter.storage.json_store import JsonRunStore

            config = load_router_config(
                Path('/fixture/config/task-models.yaml'),
                schema_path=Path('/etc/goalrouter/task-models.schema.json'),
            )
            item = WorkItem(
                id='write',
                title='Persisted',
                instructions='Already complete',
                task='python-coding',
                phase='integration',
                dependencies=(),
                access=AccessMode.WORKSPACE_WRITE,
                affected_paths=(Path('requested-write'),),
                expected_result='complete',
                verification=('persisted',),
                confidence=1.0,
                risk_flags=frozenset(),
            )
            route = TaskRouter(config).route(item)
            result = WorkResult(
                work_item_id='write',
                thread_id='thread-existing',
                turn_id='turn-existing',
                status=WorkStatus.SUCCEEDED,
                final_response='complete',
                sdk_items=(),
                input_tokens=0,
                cached_input_tokens=0,
                output_tokens=0,
                duration_seconds=0.0,
                changed_paths=(Path('requested-write'),),
                verification=('persisted',),
                confidence=1.0,
                escalation_requested=False,
                error=None,
            )
            objective = Objective(
                id='task8-persist',
                prompt='Persist',
                project_path=Path('/project'),
                explicit_task='python-coding',
                config_path=Path('/config/task-models.yaml'),
                created_at=datetime(2026, 8, 4, tzinfo=UTC),
            )
            repository = RepositoryContext(
                project_path=Path('/project'),
                is_git_worktree=False,
                branch=None,
                dirty_paths=(),
                instruction_files=(),
                language_counts=(),
                docker_files=(),
                command_errors=(),
            )
            state = RunState(
                schema_version=1,
                configuration_digest=config.digest,
                objective=objective,
                repository=repository,
                work_items={'write': item},
                routes={'write': route},
                results={'write': result},
                approvals={},
                status=RunStatus.COMPLETED,
            )
            asyncio.run(JsonRunStore(Path('/state')).create(state))
            """
        )
        _docker(
            "run",
            "--rm",
            "--user",
            "24680:24681",
            "--volume",
            f"{volume}:/fixture:ro",
            "--volume",
            f"{state_volume}:/state:rw",
            "--entrypoint",
            "python",
            base_repo_digest,
            "-c",
            seed,
        )
        secret = "task8-api-key-value-must-not-persist"
        persistent_commands = (
            ("status", "task8-persist"),
            ("approve", "task8-persist", "write", "--approved-by", "integration"),
            ("resume", "task8-persist"),
            ("report", "task8-persist"),
        )
        outputs: list[str] = []
        for command in persistent_commands:
            result = _docker(
                "run",
                "--rm",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,exec,nosuid,size=64m,mode=1777",
                "--user",
                "24680:24681",
                "--group-add",
                str(socket_gid),
                "--volume",
                "/var/run/docker.sock:/var/run/docker.sock:rw",
                "--volume",
                f"{volume}:{fixture_path}:ro",
                "--volume",
                f"{state_volume}:{state_mountpoint}:rw",
                "--env",
                "HOME=/tmp/launcher-home",
                "--env",
                f"OPENAI_API_KEY={secret}",
                "docker:28.3.3-cli@sha256:0135662b510037ea581d99c2e5929c5e01185139c0b86986a418bd4da0b98a44",
                f"{fixture_path}/goalrouter",
                "--project",
                f"{fixture_path}/project",
                "--access",
                "readonly",
                "--config",
                f"{fixture_path}/config/task-models.yaml",
                "--state-dir",
                state_mountpoint,
                "--auth-mode",
                "api-key",
                "--image",
                base_repo_digest,
                "--json",
                *command,
                check=False,
            )
            assert result.returncode == 0, result.stderr
            assert secret not in result.stdout
            assert secret not in result.stderr
            outputs.append(result.stdout)
        assert '"status": "completed"' in outputs[0]
        assert '"approved_by": "integration"' in outputs[1]
        assert '"status": "completed"' in outputs[2]
        assert '"report"' in outputs[3]
        persisted = _docker(
            "run",
            "--rm",
            "--volume",
            f"{state_volume}:/state:ro",
            "--entrypoint",
            "/bin/sh",
            "docker:28.3.3-cli@sha256:0135662b510037ea581d99c2e5929c5e01185139c0b86986a418bd4da0b98a44",
            "-eu",
            "-c",
            "test -f /state/task8-persist/report.md; "
            "grep -Fq '\"approved_by\": \"integration\"' /state/task8-persist/state.json; "
            f"! grep -R -Fq {secret!r} /state",
        )
        assert persisted.stdout == ""
    finally:
        _docker("container", "rm", "--force", init_name, check=False)
        _docker("container", "rm", "--force", registry_name, check=False)
        _docker("volume", "rm", volume, check=False)
        _docker("volume", "rm", state_volume, check=False)
        if probe_repo_digest:
            _docker("image", "rm", probe_repo_digest, check=False)
        if base_repo_digest:
            _docker("image", "rm", base_repo_digest, check=False)
        if registry_probe_tag:
            _docker("image", "rm", registry_probe_tag, check=False)
        if registry_base_tag:
            _docker("image", "rm", registry_base_tag, check=False)
        _docker("image", "rm", probe_image, check=False)
        _docker("image", "rm", base_image, check=False)


@pytest.mark.skipif(
    os.environ.get("GOALROUTER_DISTRIBUTION_INTEGRATION") != "1",
    reason="requires the declared Docker-socket integration profile",
)
def test_independent_containers_serialize_one_same_bind_project_writer() -> None:
    run_id = uuid.uuid4().hex
    owner = "org.goalrouter.test=distribution-integration"
    run_label = f"org.goalrouter.test.run={run_id}"
    project_volume = f"goalrouter-task12-project-{run_id}"
    control_volume = f"goalrouter-task12-control-{run_id}"
    holder_name = f"goalrouter-task12-holder-{run_id}"
    image = "goalrouter-test:local"
    holder_process: subprocess.Popen[str] | None = None
    holder_script = textwrap.dedent(
        """\
        import asyncio
        from pathlib import Path

        from goalrouter.locking import ProjectDirectoryWriteLease

        async def main() -> None:
            async with ProjectDirectoryWriteLease().acquire(Path("/project")):
                print("GOALROUTER_PROJECT_LEASE_READY", flush=True)
                await asyncio.Event().wait()

        asyncio.run(main())
        """
    )
    contender_script = textwrap.dedent(
        """\
        import asyncio
        import sys
        from pathlib import Path

        from goalrouter.errors import GoalRouterError
        from goalrouter.locking import ProjectDirectoryWriteLease

        async def dispatch_sdk() -> None:
            Path("/control/sdk-dispatch-marker").write_text("called", encoding="ascii")

        async def main() -> None:
            async with ProjectDirectoryWriteLease().acquire(Path("/project")):
                await dispatch_sdk()

        try:
            asyncio.run(main())
        except GoalRouterError as error:
            print(f"goalrouter: {error}", file=sys.stderr)
            raise SystemExit(error.exit_code) from error
        """
    )

    primary_error: BaseException | None = None
    process_cleanup_error: Exception | None = None
    cleanup_failures: list[str] = []
    try:
        _docker(
            "volume",
            "create",
            "--label",
            owner,
            "--label",
            run_label,
            project_volume,
            timeout_seconds=10,
        )
        _docker(
            "volume",
            "create",
            "--label",
            owner,
            "--label",
            run_label,
            control_volume,
            timeout_seconds=10,
        )
        project_mountpoint = _docker(
            "volume",
            "inspect",
            "--format",
            "{{.Mountpoint}}",
            project_volume,
            timeout_seconds=10,
        ).stdout.strip()
        control_mountpoint = _docker(
            "volume",
            "inspect",
            "--format",
            "{{.Mountpoint}}",
            control_volume,
            timeout_seconds=10,
        ).stdout.strip()
        common = (
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,exec,nosuid,size=64m,mode=1777",
            "--mount",
            f"type=bind,source={project_mountpoint},target=/project,readonly",
            "--mount",
            f"type=bind,source={control_mountpoint},target=/control",
        )
        _docker(
            "container",
            "create",
            "--name",
            holder_name,
            "--label",
            owner,
            "--label",
            run_label,
            *common,
            image,
            "python",
            "-c",
            holder_script,
            timeout_seconds=10,
        )
        holder_process = subprocess.Popen(
            ["docker", "container", "start", "--attach", holder_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert holder_process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(holder_process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=10), "holder did not acquire the lease in time"
        assert holder_process.stdout.readline().strip() == (
            "GOALROUTER_PROJECT_LEASE_READY"
        )

        busy = _docker(
            "run",
            "--rm",
            "--label",
            owner,
            "--label",
            run_label,
            *common,
            image,
            "python",
            "-c",
            contender_script,
            check=False,
            timeout_seconds=15,
        )
        assert busy.returncode == 14, busy.stderr
        assert "Project is busy" in busy.stderr
        marker_before_release = _docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--mount",
            f"type=bind,source={control_mountpoint},target=/control,readonly",
            image,
            "python",
            "-c",
            "from pathlib import Path; "
            "raise SystemExit(Path('/control/sdk-dispatch-marker').exists())",
            check=False,
            timeout_seconds=10,
        )
        assert marker_before_release.returncode == 0

        _docker(
            "container",
            "stop",
            "--time",
            "1",
            holder_name,
            timeout_seconds=10,
        )
        holder_process.communicate(timeout=10)
        assert holder_process.returncode is not None

        after_release = _docker(
            "run",
            "--rm",
            "--label",
            owner,
            "--label",
            run_label,
            *common,
            image,
            "python",
            "-c",
            contender_script,
            check=False,
            timeout_seconds=15,
        )
        assert after_release.returncode == 0, after_release.stderr
        audit = _docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--mount",
            f"type=bind,source={project_mountpoint},target=/project,readonly",
            "--mount",
            f"type=bind,source={control_mountpoint},target=/control,readonly",
            image,
            "python",
            "-c",
            "from pathlib import Path; "
            "assert not list(Path('/project').iterdir()); "
            "assert Path('/control/sdk-dispatch-marker').read_text(encoding='ascii') == 'called'",
            timeout_seconds=10,
        )
        assert audit.stdout == ""
    except BaseException as error:
        primary_error = error
    finally:
        container_removal_result = _attempt_docker_cleanup(
            "container",
            "rm",
            "--force",
            holder_name,
        )
        if holder_process is not None and holder_process.poll() is None:
            try:
                holder_process.kill()
                holder_process.communicate(timeout=10)
            except Exception as error:
                process_cleanup_error = error
        volume_removal_results = _remove_owned_volumes(
            (control_volume, project_volume)
        )
        owned_containers_result = _attempt_docker_cleanup(
            "container",
            "ls",
            "--all",
            "--quiet",
            "--filter",
            f"label={owner}",
            "--filter",
            f"label={run_label}",
        )
        owned_volumes_result = _attempt_docker_cleanup(
            "volume",
            "ls",
            "--quiet",
            "--filter",
            f"label={owner}",
            "--filter",
            f"label={run_label}",
        )

        cleanup_results = (
            ("holder container removal", container_removal_result),
            ("control volume removal", volume_removal_results[0]),
            ("project volume removal", volume_removal_results[1]),
            ("owned container audit", owned_containers_result),
            ("owned volume audit", owned_volumes_result),
        )
        cleanup_failures.extend(
            failure
            for action, result in cleanup_results
            if (failure := _cleanup_failure(action, result)) is not None
        )
        if process_cleanup_error is not None:
            cleanup_failures.append(
                "holder attach-process cleanup raised "
                f"{type(process_cleanup_error).__name__}: {process_cleanup_error}"
            )
        if (
            isinstance(owned_containers_result, subprocess.CompletedProcess)
            and owned_containers_result.returncode == 0
            and owned_containers_result.stdout.strip()
        ):
            cleanup_failures.append(
                "owned container audit retained: "
                f"{owned_containers_result.stdout.strip()}"
            )
        if (
            isinstance(owned_volumes_result, subprocess.CompletedProcess)
            and owned_volumes_result.returncode == 0
            and owned_volumes_result.stdout.strip()
        ):
            cleanup_failures.append(
                f"owned volume audit retained: {owned_volumes_result.stdout.strip()}"
            )

    if primary_error is not None:
        for failure in cleanup_failures:
            primary_error.add_note(f"Task 3 distribution cleanup: {failure}")
        raise primary_error
    assert cleanup_failures == []
