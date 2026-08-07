# SPDX-License-Identifier: MIT
# File: tests/distribution/test_posix_launcher.py
# Purpose: Enforce POSIX launcher argv, validation, and authority boundaries

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest
import yaml

LAUNCHER: Final = Path("scripts/goalrouter").resolve()
FAKE_DOCKER: Final = Path("tests/fixtures/distribution/fake-docker").resolve()
DIGEST: Final = "sha256:" + ("a" * 64)
SECRET: Final = "test-api-key-that-must-never-be-recorded"
PUBLIC_CONTRACT: Final = Path(
    "tests/fixtures/distribution/public-launcher-contract.json"
).resolve()


@dataclass(frozen=True, slots=True)
class LauncherHome:
    root: Path
    home: Path
    project: Path
    config: Path
    state: Path
    codex_home: Path


def make_launcher_home(root: Path) -> LauncherHome:
    home = root / "isolated-home"
    project = root / "repo"
    config = home / ".config" / "goalrouter" / "task-models.yaml"
    state = home / ".local" / "state" / "goalrouter"
    codex_home = home / ".codex"

    project.mkdir(parents=True)
    config.parent.mkdir(parents=True)
    config.write_text("schema-version: 1\n", encoding="utf-8")
    state.mkdir(parents=True)
    (state / "image-ref").write_text("ghcr.io/example/goalrouter", encoding="utf-8")
    (state / "image-digest").write_text(DIGEST, encoding="utf-8")
    codex_home.mkdir(parents=True)
    (codex_home / "auth.json").write_text("{}\n", encoding="utf-8")
    return LauncherHome(root, home, project, config, state, codex_home)


def run_posix_launcher(
    fixture: LauncherHome,
    *arguments: str,
    cwd: Path | None = None,
    extra_environment: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[bytes], list[str]]:
    fake_bin = fixture.root / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    fake_docker = fake_bin / "docker"
    fake_docker.symlink_to(FAKE_DOCKER)
    argv_file = fixture.root / "docker.argv"
    environment = {
        "FAKE_DOCKER_ARGV": str(argv_file),
        "HOME": str(fixture.home),
        "PATH": f"{fake_bin}:/usr/local/bin:/usr/bin:/bin",
    }
    if extra_environment is not None:
        environment.update(extra_environment)

    result = subprocess.run(
        [str(LAUNCHER), *arguments],
        cwd=cwd or fixture.project,
        env=environment,
        capture_output=True,
        check=False,
    )
    raw_argv = argv_file.read_bytes() if argv_file.exists() else b""
    encoded_argv = raw_argv.split(b"\0")[:-1] if raw_argv else []
    argv = [value.decode("utf-8") for value in encoded_argv]
    return result, argv


def mounts(argv: list[str]) -> list[str]:
    return [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "--mount"]


def mount_for(argv: list[str], destination: str) -> str:
    matches = [mount for mount in mounts(argv) if f",dst={destination}" in mount]
    assert len(matches) == 1
    return matches[0]


def env_values(argv: list[str]) -> list[str]:
    return [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "--env"]


def test_posix_launcher_consumes_shared_public_option_contract() -> None:
    contract = json.loads(PUBLIC_CONTRACT.read_text(encoding="utf-8"))
    result = subprocess.run(
        [str(LAUNCHER), "--help"],
        env={"HOME": "/tmp/help-contract", "PATH": "/usr/local/bin:/usr/bin:/bin"},
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert re.findall(r"^  (--[a-z-]+)", result.stdout, re.MULTILINE) == contract[
        "options"
    ]
    assert re.findall(
        r"^  (doctor|update|version|uninstall)\b", result.stdout, re.MULTILINE
    ) == contract["maintenance_commands"]


def test_posix_help_precedes_home_and_leading_value_validation() -> None:
    for arguments in (("--help",), ("--access", "WRITE", "--help")):
        result = subprocess.run(
            [str(LAUNCHER), *arguments],
            env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
            capture_output=True,
            check=False,
            text=True,
        )

        assert result.returncode == 0
        assert result.stderr == ""
        assert "Usage: goalrouter" in result.stdout


def test_posix_help_after_python_command_is_forwarded(tmp_path: Path) -> None:
    fixture = make_launcher_home(tmp_path)

    result, argv = run_posix_launcher(fixture, "version", "--help")

    assert result.returncode == 0
    assert argv[-3:] == [f"ghcr.io/example/goalrouter@{DIGEST}", "version", "--help"]


def test_source_checkout_launcher_accepts_relative_dot_path(tmp_path: Path) -> None:
    fixture = make_launcher_home(tmp_path)
    fake_bin = fixture.root / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "docker").symlink_to(FAKE_DOCKER)
    argv_file = fixture.root / "docker.argv"

    result = subprocess.run(
        ["./scripts/goalrouter", "--project", str(fixture.project), "config", "validate"],
        cwd=Path.cwd(),
        env={
            "FAKE_DOCKER_ARGV": str(argv_file),
            "HOME": str(fixture.home),
            "PATH": f"{fake_bin}:/usr/local/bin:/usr/bin:/bin",
        },
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert f"ghcr.io/example/goalrouter@{DIGEST}" in argv_file.read_text(encoding="utf-8")


def test_readonly_launcher_builds_minimal_container_authority(tmp_path: Path) -> None:
    fixture = make_launcher_home(tmp_path)

    result, argv = run_posix_launcher(
        fixture, "--project", str(fixture.project), "version"
    )

    assert result.returncode == 0
    assert argv[:4] == ["run", "--rm", "--read-only", "--tmpfs"]
    assert argv[4] == "/tmp:rw,exec,nosuid,size=1g,mode=1777"
    assert mount_for(argv, "/project") == (
        f"type=bind,src={fixture.project},dst=/project,readonly"
    )
    assert mount_for(argv, "/state") == f"type=bind,src={fixture.state},dst=/state"
    assert mount_for(argv, "/config/task-models.yaml") == (
        f"type=bind,src={fixture.config},dst=/config/task-models.yaml,readonly"
    )
    assert mount_for(argv, "/codex-auth") == (
        f"type=bind,src={fixture.codex_home},dst=/codex-auth,readonly"
    )
    assert "/var/run/docker.sock:/var/run/docker.sock:rw" not in argv
    assert env_values(argv) == [
        "GOALROUTER_CONFIG=/config/task-models.yaml",
        "GOALROUTER_STATE_PATH=/state",
        "GOALROUTER_AUTH_MODE=existing-session",
        "GOALROUTER_CODEX_HOME=/codex-auth",
        "GOALROUTER_CODEX_STAGING_PATH=/tmp/codex-home",
    ]
    assert argv[argv.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert argv[-2:] == [f"ghcr.io/example/goalrouter@{DIGEST}", "version"]
    assert SECRET.encode() not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("access", "readonly", "has_socket"),
    [("readonly", True, False), ("write", False, False), ("docker", False, True)],
)
def test_authority_mode_is_exact(
    tmp_path: Path, access: str, readonly: bool, has_socket: bool
) -> None:
    fixture = make_launcher_home(tmp_path)

    result, argv = run_posix_launcher(fixture, "--access", access, "version")

    assert result.returncode == 0
    assert mount_for(argv, "/project").endswith(",readonly") is readonly
    assert (
        "/var/run/docker.sock:/var/run/docker.sock:rw" in argv
    ) is has_socket


def test_path_overrides_preserve_spaces_and_newlines_as_single_argv(tmp_path: Path) -> None:
    fixture = make_launcher_home(tmp_path)
    project = tmp_path / "project with space\nand newline"
    config = tmp_path / "config with space\nand newline.yaml"
    state = tmp_path / "state with space\nand newline"
    codex_home = tmp_path / "codex with space\nand newline"
    project.mkdir()
    config.write_text("schema-version: 1\n", encoding="utf-8")
    state.mkdir()
    codex_home.mkdir()

    result, argv = run_posix_launcher(
        fixture,
        "--project",
        str(project),
        "--config",
        str(config),
        "--state-dir",
        str(state),
        "--codex-home",
        str(codex_home),
        "--image",
        "goalrouter-runtime:local",
        "version",
    )

    assert result.returncode == 0
    assert mount_for(argv, "/project") == (
        f"type=bind,src={project},dst=/project,readonly"
    )
    assert mount_for(argv, "/config/task-models.yaml") == (
        f"type=bind,src={config},dst=/config/task-models.yaml,readonly"
    )
    assert mount_for(argv, "/state") == f"type=bind,src={state},dst=/state"
    assert mount_for(argv, "/codex-auth") == (
        f"type=bind,src={codex_home},dst=/codex-auth,readonly"
    )
    assert argv[-2:] == ["goalrouter-runtime:local", "version"]


@pytest.mark.parametrize(
    "suffix", [" ", "\n", "\n\n", " embedded\nnewline "]
)
def test_project_path_preserves_every_byte_including_trailing_newlines(
    tmp_path: Path, suffix: str
) -> None:
    fixture = make_launcher_home(tmp_path)
    project = tmp_path / f"project{suffix}"
    project.mkdir()

    result, argv = run_posix_launcher(
        fixture, "--project", str(project), "--access", "write", "version"
    )

    assert result.returncode == 0
    assert mount_for(argv, "/project") == f"type=bind,src={project},dst=/project"


@pytest.mark.parametrize(
    "suffix", [" ", "\n", "\n\n", " embedded\nnewline "]
)
def test_config_path_preserves_every_byte_including_trailing_newlines(
    tmp_path: Path, suffix: str
) -> None:
    fixture = make_launcher_home(tmp_path)
    config = tmp_path / f"config{suffix}"
    config.write_text("schema-version: 1\n", encoding="utf-8")

    result, argv = run_posix_launcher(
        fixture, "--config", str(config), "version"
    )

    assert result.returncode == 0
    assert mount_for(argv, "/config/task-models.yaml") == (
        f"type=bind,src={config},dst=/config/task-models.yaml,readonly"
    )


def test_directory_symlinks_are_resolved_physically(tmp_path: Path) -> None:
    fixture = make_launcher_home(tmp_path)
    physical = tmp_path / "physical"
    project = physical / "project"
    state = physical / "state"
    codex_home = physical / "codex"
    config = physical / "config\n\n"
    project.mkdir(parents=True)
    state.mkdir()
    codex_home.mkdir()
    config.write_text("schema-version: 1\n", encoding="utf-8")
    link = tmp_path / "linked"
    link.symlink_to(physical, target_is_directory=True)

    result, argv = run_posix_launcher(
        fixture,
        "--project",
        str(link / "project"),
        "--config",
        str(link / "config\n\n"),
        "--state-dir",
        str(link / "state"),
        "--codex-home",
        str(link / "codex"),
        "--image",
        "goalrouter-runtime:local",
        "version",
    )

    assert result.returncode == 0
    assert f"src={physical / 'project'},dst=/project" in mount_for(argv, "/project")
    assert mount_for(argv, "/state") == f"type=bind,src={physical / 'state'},dst=/state"
    assert f"src={physical / 'codex'},dst=/codex-auth" in mount_for(argv, "/codex-auth")
    assert mount_for(argv, "/config/task-models.yaml") == (
        f"type=bind,src={config},dst=/config/task-models.yaml,readonly"
    )


def test_default_project_is_physical_current_directory(tmp_path: Path) -> None:
    fixture = make_launcher_home(tmp_path)
    link = tmp_path / "project-link"
    link.symlink_to(fixture.project, target_is_directory=True)

    result, argv = run_posix_launcher(fixture, "version", cwd=link)

    assert result.returncode == 0
    assert f"src={fixture.project},dst=/project" in mount_for(argv, "/project")


def test_xdg_defaults_are_honored(tmp_path: Path) -> None:
    fixture = make_launcher_home(tmp_path)
    xdg_config = tmp_path / "xdg config"
    xdg_state = tmp_path / "xdg state"
    config = xdg_config / "goalrouter" / "task-models.yaml"
    state = xdg_state / "goalrouter"
    config.parent.mkdir(parents=True)
    config.write_text("schema-version: 1\n", encoding="utf-8")
    state.mkdir(parents=True)
    (state / "image-ref").write_text("example/goalrouter", encoding="utf-8")
    (state / "image-digest").write_text(DIGEST, encoding="utf-8")

    result, argv = run_posix_launcher(
        fixture,
        "version",
        extra_environment={
            "XDG_CONFIG_HOME": str(xdg_config),
            "XDG_STATE_HOME": str(xdg_state),
        },
    )

    assert result.returncode == 0
    assert f"src={config},dst=/config/task-models.yaml" in mount_for(
        argv, "/config/task-models.yaml"
    )
    assert mount_for(argv, "/state") == f"type=bind,src={state},dst=/state"


def test_codex_home_environment_default_is_honored(tmp_path: Path) -> None:
    fixture = make_launcher_home(tmp_path)
    codex_home = tmp_path / "explicit environment codex"
    codex_home.mkdir()

    result, argv = run_posix_launcher(
        fixture,
        "version",
        extra_environment={"CODEX_HOME": str(codex_home)},
    )

    assert result.returncode == 0
    assert f"src={codex_home},dst=/codex-auth" in mount_for(argv, "/codex-auth")


def test_json_flag_reaches_python_cli(tmp_path: Path) -> None:
    fixture = make_launcher_home(tmp_path)

    result, argv = run_posix_launcher(fixture, "--json", "version")

    assert result.returncode == 0
    assert argv[-3:] == [f"ghcr.io/example/goalrouter@{DIGEST}", "--json", "version"]


def test_api_key_mode_forwards_only_the_variable_name(tmp_path: Path) -> None:
    fixture = make_launcher_home(tmp_path)
    shutil.rmtree(fixture.codex_home)

    result, argv = run_posix_launcher(
        fixture,
        "--auth-mode",
        "api-key",
        "version",
        extra_environment={"OPENAI_API_KEY": SECRET},
    )

    assert result.returncode == 0
    assert all("/codex-auth" not in value for value in argv)
    assert env_values(argv) == [
        "GOALROUTER_CONFIG=/config/task-models.yaml",
        "GOALROUTER_STATE_PATH=/state",
        "GOALROUTER_AUTH_MODE=api-key",
        "OPENAI_API_KEY",
    ]
    combined_output = result.stdout + result.stderr
    assert SECRET not in "\0".join(argv)
    assert SECRET.encode() not in combined_output


def test_api_key_mode_requires_a_nonempty_variable(tmp_path: Path) -> None:
    fixture = make_launcher_home(tmp_path)

    result, argv = run_posix_launcher(
        fixture,
        "--auth-mode",
        "api-key",
        "version",
        extra_environment={"OPENAI_API_KEY": ""},
    )

    assert result.returncode != 0
    assert argv == []
    assert b"OPENAI_API_KEY is required" in result.stderr


@pytest.mark.parametrize(
    ("arguments", "expected_error"),
    [
        (("--access", "admin", "version"), b"invalid --access"),
        (("--auth-mode", "fallback", "version"), b"invalid --auth-mode"),
        (("--unknown", "version"), b"unknown launcher option"),
        (("--project",), b"--project requires a value"),
        (("--access",), b"--access requires a value"),
        (("--config",), b"--config requires a value"),
        (("--state-dir",), b"--state-dir requires a value"),
        (("--codex-home",), b"--codex-home requires a value"),
        (("--image",), b"--image requires a value"),
        (("--auth-mode",), b"--auth-mode requires a value"),
    ],
)
def test_unknown_invalid_and_incomplete_options_fail_explicitly(
    tmp_path: Path, arguments: tuple[str, ...], expected_error: bytes
) -> None:
    fixture = make_launcher_home(tmp_path)

    result, argv = run_posix_launcher(fixture, *arguments)

    assert result.returncode != 0
    assert argv == []
    assert expected_error in result.stderr


@pytest.mark.parametrize("kind", ["project", "config", "state", "codex"])
def test_missing_required_paths_fail_explicitly(tmp_path: Path, kind: str) -> None:
    fixture = make_launcher_home(tmp_path)
    missing = tmp_path / f"missing-{kind}"
    options = {
        "project": ("--project", str(missing)),
        "config": ("--config", str(missing)),
        "state": ("--state-dir", str(missing)),
        "codex": ("--codex-home", str(missing)),
    }

    result, argv = run_posix_launcher(fixture, *options[kind], "version")

    assert result.returncode != 0
    assert argv == []
    assert f"{kind} ".encode() in result.stderr
    assert b"does not exist" in result.stderr


@pytest.mark.parametrize(
    ("filename", "content", "expected_error"),
    [
        ("image-ref", b"", b"invalid image-ref metadata bytes"),
        (
            "image-ref",
            b"example/goalrouter\x00ignored",
            b"invalid image-ref metadata bytes",
        ),
        ("image-ref", b"example/goalrouter\n", b"invalid image-ref metadata bytes"),
        ("image-ref", b"example/goalrouter\n\n", b"invalid image-ref metadata bytes"),
        ("image-ref", b"example/goalrouter\r", b"invalid image-ref metadata bytes"),
        ("image-ref", b"example/goalrouter\t", b"invalid image-ref metadata bytes"),
        ("image-ref", b"example/goalrouter\x7f", b"invalid image-ref metadata bytes"),
        (
            "image-ref",
            "example/goalrouter\nsecond",
            b"invalid image-ref metadata bytes",
        ),
        (
            "image-ref",
            "example/goalrouter\x01",
            b"invalid image-ref metadata bytes",
        ),
        ("image-ref", "example/goalrouter@latest", b"invalid image reference"),
        ("image-digest", "sha256:" + ("a" * 63), b"invalid image digest"),
        ("image-digest", "sha256:" + ("A" * 64), b"invalid image digest"),
        (
            "image-digest",
            DIGEST + "\nsecond",
            b"invalid image-digest metadata bytes",
        ),
    ],
)
def test_installed_image_metadata_is_strictly_validated(
    tmp_path: Path, filename: str, content: str | bytes, expected_error: bytes
) -> None:
    fixture = make_launcher_home(tmp_path)
    metadata = fixture.state / filename
    if isinstance(content, bytes):
        metadata.write_bytes(content)
    else:
        metadata.write_text(content, encoding="utf-8")

    result, argv = run_posix_launcher(fixture, "version")

    assert result.returncode != 0
    assert argv == []
    assert expected_error in result.stderr


@pytest.mark.parametrize("filename", ["image-ref", "image-digest"])
def test_missing_installed_image_metadata_fails_explicitly(
    tmp_path: Path, filename: str
) -> None:
    fixture = make_launcher_home(tmp_path)
    (fixture.state / filename).unlink()

    result, argv = run_posix_launcher(fixture, "version")

    assert result.returncode != 0
    assert argv == []
    assert f"missing {filename}".encode() in result.stderr


@pytest.mark.parametrize(
    "image", ["", "--privileged", "goalrouter runtime:local", "goalrouter\nruntime:local"]
)
def test_explicit_image_override_is_validated(tmp_path: Path, image: str) -> None:
    fixture = make_launcher_home(tmp_path)

    result, argv = run_posix_launcher(fixture, "--image", image, "version")

    assert result.returncode != 0
    assert argv == []
    assert b"invalid --image" in result.stderr


def test_explicit_image_override_does_not_require_installed_metadata(tmp_path: Path) -> None:
    fixture = make_launcher_home(tmp_path)
    (fixture.state / "image-ref").unlink()
    (fixture.state / "image-digest").unlink()

    result, argv = run_posix_launcher(
        fixture, "--image", "goalrouter-runtime:local", "version"
    )

    assert result.returncode == 0
    assert argv[-2:] == ["goalrouter-runtime:local", "version"]


@pytest.mark.parametrize(
    "image",
    [
        "goalrouter",
        "goalrouter-runtime:local",
        "registry.example.com:5000/team/goalrouter:release_1",
        f"registry.example.com/team/goalrouter@{DIGEST}",
        DIGEST,
    ],
)
def test_explicit_image_accepts_conservative_docker_oci_references(
    tmp_path: Path, image: str
) -> None:
    fixture = make_launcher_home(tmp_path)

    result, argv = run_posix_launcher(fixture, "--image", image, "version")

    assert result.returncode == 0
    assert argv[-2:] == [image, "version"]


@pytest.mark.parametrize(
    "image",
    [
        "team/my--image",
        "team/my__image",
        "localhost:5000/team/image:tag",
        "REGISTRY.Example.COM:5000/team/image:Release_1",
        "[2001:db8::1]:5000/team/image:tag",
        "a" * 255,
        "repo/image:" + ("a" * 128),
        f"repo/image:tag@{DIGEST}",
    ],
)
def test_explicit_image_accepts_distribution_reference_boundaries(
    tmp_path: Path, image: str
) -> None:
    fixture = make_launcher_home(tmp_path)

    result, argv = run_posix_launcher(fixture, "--image", image, "version")

    assert result.returncode == 0
    assert argv[-2:] == [image, "version"]


@pytest.mark.parametrize(
    "image",
    [
        ":",
        "@",
        "repo/",
        "/repo",
        "repo//image",
        "repo..name/image",
        "Repo/image",
        "repo/image:",
        "repo/image:bad tag",
        "repo/image@@sha256:" + ("a" * 64),
        "repo/image@sha256:" + ("a" * 63),
        "repo/image@sha256:" + ("A" * 64),
        "repo/image@md5:" + ("a" * 32),
        "registry.example.com:port/repo/image",
        "bad_host.example/repo/image",
        ".registry.example/repo/image",
        "registry..example/repo/image",
        "team/my___image",
        "team/my..image",
        "a" * 256,
        "repo/image:" + ("a" * 129),
        "[2001:db8::1/repo/image",
        "[2001:db8::1]:port/repo/image",
        "REGISTRY.Example.COM/Team/image",
    ],
)
def test_explicit_image_rejects_invalid_docker_oci_references(
    tmp_path: Path, image: str
) -> None:
    fixture = make_launcher_home(tmp_path)

    result, argv = run_posix_launcher(fixture, "--image", image, "version")

    assert result.returncode != 0
    assert argv == []
    assert b"invalid --image" in result.stderr


def test_launcher_and_fake_are_executable_in_the_declared_test_image() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert os.access(LAUNCHER, os.X_OK)
    assert os.access(FAKE_DOCKER, os.X_OK)
    assert "RUN chmod 0555" in dockerfile
    assert "scripts/goalrouter" in dockerfile
    assert "scripts/docker-resource-cleanup.sh" in dockerfile
    assert "tests/fixtures/distribution/fake-docker" in dockerfile


def test_posix_launcher_smoke_has_a_least_authority_declared_boundary() -> None:
    compose = yaml.safe_load(Path("compose.yaml").read_text(encoding="utf-8"))
    service = compose["services"].get("posix-launcher-smoke")

    assert service is not None
    assert service["image"] == "docker:28.3.3-cli@sha256:0135662b510037ea581d99c2e5929c5e01185139c0b86986a418bd4da0b98a44"  # noqa: E501
    assert service["entrypoint"] == [
        "/bin/sh",
        "/workspace/scripts/posix-launcher-smoke.sh",
    ]
    assert service["network_mode"] == "none"
    assert service["read_only"] is True
    assert service["tmpfs"] == ["/tmp:rw,exec,nosuid,size=256m,mode=1777"]
    assert set(service["volumes"]) == {
        ".:/workspace:ro",
        "/var/run/docker.sock:/var/run/docker.sock:rw",
    }

    harness = Path("scripts/posix-launcher-smoke.sh").read_text(encoding="utf-8")
    required_contracts = {
        "org.goalrouter.test=posix-launcher-smoke",
        "goalrouter-runtime:local",
        'docker volume inspect --format \'{{.Mountpoint}}\'',
        '"$fixture_mountpoint/goalrouter"',
        "--json version",
        "protocol_version",
        "immutable_before",
        "immutable_after",
        'gr_verify_volume "$fixture_volume"',
        "built_image_id",
        "reused_image_id",
        'gr_cleanup_register_image "$image" "$built_image_id"',
        "gr_cleanup_owned_resources",
    }
    assert {value for value in required_contracts if value not in harness} == set()
    assert '/bin/sh "$fixture_mountpoint/goalrouter"' not in harness
    acquisition = harness.index('docker volume create \\\n')
    ownership = harness.index('gr_verify_volume "$fixture_volume"', acquisition)
    first_mount = harness.index('docker container create \\\n', acquisition)
    assert acquisition < ownership < first_mount

    safety_service = compose["services"].get("posix-launcher-smoke-safety")
    assert safety_service is not None
    assert safety_service["entrypoint"] == [
        "/bin/sh",
        "/workspace/scripts/posix-launcher-smoke-safety.sh",
    ]
    assert safety_service["network_mode"] == "none"
    assert safety_service["read_only"] is True
    assert set(safety_service["volumes"]) == {
        ".:/workspace:ro",
        "/var/run/docker.sock:/var/run/docker.sock:rw",
    }
    safety_harness = Path("scripts/posix-launcher-smoke-safety.sh").read_text(
        encoding="utf-8"
    )
    for contract in {
        "collision-sentinel",
        "expected ownership refusal",
        "preexisting_image_id",
        "preserved image tag or ID",
        'gr_verify_volume "$collision_volume"',
        'gr_cleanup_register_image "$image" "$created_image_id"',
        "gr_cleanup_owned_resources",
    }:
        assert contract in safety_harness

    shellcheck = compose["services"]["shellcheck"]["command"]
    assert shellcheck == [
        "shellcheck",
        "-x",
        "scripts/container-entrypoint.sh",
        "scripts/docker-resource-cleanup.sh",
        "scripts/runtime-image-smoke.sh",
        "scripts/runtime-image-smoke-interrupt.sh",
        "scripts/goalrouter",
        "scripts/install.sh",
        "scripts/uninstall.sh",
        "scripts/posix-installer-smoke.sh",
        "scripts/posix-launcher-smoke.sh",
        "scripts/posix-launcher-smoke-safety.sh",
        "scripts/release-assets.sh",
        "tests/fixtures/distribution/fake-docker",
        "tests/fixtures/distribution/fake-cleanup-docker",
        "tests/fixtures/distribution/fake-release/docker",
    ]
