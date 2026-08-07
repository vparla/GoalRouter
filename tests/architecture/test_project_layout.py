# SPDX-License-Identifier: MIT
# File: tests/architecture/test_project_layout.py
# Purpose: Enforce GoalRouter's Python and package layout baseline

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_python_runtime_and_package_layout() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert 'requires-python = ">=3.14,<3.15"' in pyproject
    assert (
        "FROM python:3.14.6-alpine3.24@sha256:"
        "26730869004e2b9c4b9ad09cab8625e81d256d1ce97e72df5520e806b1709f92 AS base"
        in dockerfile
    )
    assert "PIP_ROOT_USER_ACTION=ignore" in dockerfile
    assert (ROOT / "src/goalrouter").is_dir()
    assert (ROOT / "src/goalrouter/sdk").is_dir()
    assert (ROOT / "src/goalrouter/storage").is_dir()


def test_local_directories_are_ignored() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert "planning/" in ignored
    assert ".goalrouter/" in ignored
    assert ".superpowers/" in ignored


def test_verification_tool_caches_use_the_tmpfs() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "image: goalrouter-test:local" in compose
    assert '"--no-cache"' in compose
    assert '"--cache-dir", "/tmp/mypy"' in compose


def test_package_and_live_smoke_have_declared_compose_services() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    live = (ROOT / "compose.live.yaml").read_text(encoding="utf-8")

    assert "package:" in compose
    assert '"wheel"' in compose
    assert "live-test:" in live
    assert "GOALROUTER_LIVE_TEST" in live
    live_test = live.split("live-test:", maxsplit=1)[1].split(
        "live-inventory:", maxsplit=1
    )[0]
    assert "docker.sock" not in live_test
