# SPDX-License-Identifier: MIT
# File: tests/distribution/test_runtime_image.py
# Purpose: Enforce the portable OCI runtime and live-Compose authority boundary

from pathlib import Path

import yaml


def test_runtime_declares_oci_identity_and_portable_home() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert (
        "FROM python:3.14.6-alpine3.24@sha256:"
        "26730869004e2b9c4b9ad09cab8625e81d256d1ce97e72df5520e806b1709f92 AS base"
        in dockerfile
    )
    assert "ARG VERSION" in dockerfile
    assert "ARG REVISION" in dockerfile
    assert "ARG CREATED" in dockerfile
    assert 'org.opencontainers.image.source="https://github.com/vparla/GoalRouter"' in dockerfile
    assert 'org.opencontainers.image.licenses="MIT"' in dockerfile
    assert 'org.opencontainers.image.version="${VERSION}"' in dockerfile
    assert 'org.opencontainers.image.revision="${REVISION}"' in dockerfile
    assert 'org.opencontainers.image.created="${CREATED}"' in dockerfile
    assert "GOALROUTER_IMAGE_VERSION=${VERSION}" in dockerfile
    assert "GOALROUTER_IMAGE_REVISION=${REVISION}" in dockerfile
    assert "GOALROUTER_IMAGE_CREATED=${CREATED}" in dockerfile
    assert "HOME=/tmp/goalrouter-home" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert 'ENTRYPOINT ["/usr/local/bin/goalrouter-container-entrypoint"]' in dockerfile


def test_runtime_entrypoint_has_exact_ownership_independent_setup() -> None:
    entrypoint = Path("scripts/container-entrypoint.sh").read_text(encoding="utf-8")

    assert entrypoint == (
        "#!/bin/sh\n"
        "set -eu\n"
        "umask 077\n"
        'mkdir -p "${HOME:?}" "${CODEX_HOME:?}"\n'
        'exec python -m goalrouter "$@"\n'
    )


def test_live_compose_preserves_mount_authority_boundaries() -> None:
    compose = yaml.safe_load(Path("compose.live.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    authority_services = {"cli-readonly", "cli-write", "cli-docker"}

    assert authority_services <= services.keys()
    for name in authority_services:
        service = services[name]
        volumes = set(service["volumes"])
        assert any(volume.endswith(":/config/task-models.yaml:ro") for volume in volumes)
        assert any(volume.endswith(":/codex-auth:ro") for volume in volumes)
        assert any(volume.endswith(":/state:rw") for volume in volumes)
        assert service["read_only"] is True
        assert "/tmp:rw,exec,nosuid,size=1g,mode=1777" in service["tmpfs"]

    assert any(
        volume.endswith(":/project:ro") for volume in services["cli-readonly"]["volumes"]
    )
    assert any(volume.endswith(":/project:rw") for volume in services["cli-write"]["volumes"])
    assert any(volume.endswith(":/project:rw") for volume in services["cli-docker"]["volumes"])

    socket_mount = "/var/run/docker.sock:/var/run/docker.sock:rw"
    assert socket_mount not in services["cli-readonly"]["volumes"]
    assert socket_mount not in services["cli-write"]["volumes"]
    assert socket_mount in services["cli-docker"]["volumes"]


def test_live_compose_forwards_runtime_build_metadata() -> None:
    compose = yaml.safe_load(Path("compose.live.yaml").read_text(encoding="utf-8"))
    build_args = compose["x-cli-common"]["build"]["args"]

    assert build_args == {
        "VERSION": "${VERSION:-1.0.7}",
        "REVISION": "${REVISION:-local}",
        "CREATED": "${CREATED:-1970-01-01T00:00:00Z}",
    }


def test_runtime_smoke_is_a_declared_least_authority_docker_gate() -> None:
    compose = yaml.safe_load(Path("compose.yaml").read_text(encoding="utf-8"))
    service = compose["services"].get("runtime-smoke")

    assert service is not None
    assert service["image"] == "docker:28.3.3-cli@sha256:0135662b510037ea581d99c2e5929c5e01185139c0b86986a418bd4da0b98a44"  # noqa: E501
    assert service["entrypoint"] == ["/bin/sh", "/workspace/scripts/runtime-image-smoke.sh"]
    assert service["network_mode"] == "none"
    assert service["read_only"] is True
    assert service["tmpfs"] == ["/tmp:rw,exec,nosuid,size=256m,mode=1777"]
    assert set(service["volumes"]) == {
        ".:/workspace:ro",
        "/var/run/docker.sock:/var/run/docker.sock:rw",
    }

    harness = Path("scripts/runtime-image-smoke.sh").read_text(encoding="utf-8")
    required_contracts = {
        "docker build",
        "docker image inspect",
        "--user 24680:24681",
        "getent passwd 24680",
        "--read-only",
        "--tmpfs /tmp:rw,exec,nosuid",
        "config template",
        "/state/ownership-probe",
        "/project/immutable.txt",
        "/config/task-models.yaml",
        "/codex-auth/auth.json",
    }
    missing_contracts = {contract for contract in required_contracts if contract not in harness}

    assert missing_contracts == set()


def test_runtime_uses_a_clean_multistage_dependency_boundary() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert (
        "FROM python:3.14.6-alpine3.24@sha256:"
        "26730869004e2b9c4b9ad09cab8625e81d256d1ce97e72df5520e806b1709f92 AS base"
        in dockerfile
    )
    assert "FROM base AS build" in dockerfile
    assert "RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels ." in dockerfile
    runtime = dockerfile.split("FROM base AS runtime", maxsplit=1)[1]
    assert "COPY --from=build /wheels /wheels" in runtime
    assert "ca-certificates git" in runtime
    assert "apk add --no-cache" in runtime
    assert "apt-get" not in runtime
    assert "addgroup -g 10001 -S goalrouter" in runtime
    assert "adduser -u 10001 -S -D -H -G goalrouter goalrouter" in runtime
    assert "COPY src" not in runtime
    assert "COPY tests" not in runtime
    assert "/usr/local/bin/pip" in runtime
    assert "/usr/local/lib/python3.14/ensurepip" in runtime

    harness = Path("scripts/runtime-image-smoke.sh").read_text(encoding="utf-8")
    runtime_exclusions = {
        "test -x /bin/sh",
        "test -r /etc/ssl/certs/ca-certificates.crt",
        "command -v git",
        "pip pip3 pytest ruff mypy gcc cc make docker",
        "test ! -e /workspace/src",
        "test ! -e /workspace/tests",
    }
    missing_exclusions = {
        exclusion for exclusion in runtime_exclusions if exclusion not in harness
    }

    assert missing_exclusions == set()


def test_runtime_smoke_cleanup_is_ownership_scoped_and_signal_safe() -> None:
    harness = Path("scripts/runtime-image-smoke.sh").read_text(encoding="utf-8")
    helper = Path("scripts/docker-resource-cleanup.sh").read_text(encoding="utf-8")

    assert "owner_label='org.goalrouter.test=runtime-image-smoke'" in harness
    assert 'run_label="org.goalrouter.test.run=$smoke_run_id"' in harness
    assert '--label "$owner_label"' in harness
    assert '--label "$run_label"' in harness
    assert 'gr_cleanup_init runtime-image-smoke "$smoke_run_id"' in harness
    assert 'gr_cleanup_register_image "$image" "$runtime_image_id"' in harness
    assert "gr_cleanup_owned_resources" in harness
    assert "docker container inspect" in helper
    assert "docker volume inspect" in helper
    assert "docker image inspect" in helper
    assert "GR_CLEANUP_FAILED=1" in helper
    assert "|| true" not in helper

    containers = helper.index("docker container ls --all --quiet")
    volumes = helper.index("docker volume ls --quiet")
    images = helper.index("docker image ls --quiet")
    assert containers < volumes < images

    assert "trap 'gr_handle_signal 129' HUP" in helper
    assert "trap 'gr_handle_signal 130' INT" in helper
    assert "trap 'gr_handle_signal 143' TERM" in helper
    assert "trap 'gr_handle_exit' EXIT" in helper

    compose = yaml.safe_load(Path("compose.yaml").read_text(encoding="utf-8"))
    service = compose["services"].get("runtime-smoke-interrupt")
    assert service is not None
    assert service["image"] == "docker:28.3.3-cli@sha256:0135662b510037ea581d99c2e5929c5e01185139c0b86986a418bd4da0b98a44"  # noqa: E501
    assert service["entrypoint"] == [
        "/bin/sh",
        "/workspace/scripts/runtime-image-smoke-interrupt.sh",
    ]
    assert service["network_mode"] == "none"
    assert service["read_only"] is True
    assert set(service["volumes"]) == {
        ".:/workspace:ro",
        "/var/run/docker.sock:/var/run/docker.sock:rw",
    }

    shellcheck = compose["services"]["shellcheck"]
    assert shellcheck["working_dir"] == "/workspace"
    assert shellcheck["volumes"] == [".:/workspace:ro"]
    assert shellcheck["command"] == [
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

    interruption = Path("scripts/runtime-image-smoke-interrupt.sh").read_text(
        encoding="utf-8"
    )
    assert "docker events" in interruption
    assert "docker kill --signal TERM" in interruption
    assert "expected signal status 143" in interruption
    assert "post-signal work continued" in interruption
