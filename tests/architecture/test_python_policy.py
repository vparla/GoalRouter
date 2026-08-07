# SPDX-License-Identifier: MIT
# File: tests/architecture/test_python_policy.py
# Purpose: Enforce Python, async, shell, and SDK import architecture policy

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src/goalrouter"


def _python_files() -> tuple[Path, ...]:
    return tuple(sorted(SOURCE.rglob("*.py")))


def test_forbidden_compatibility_and_async_apis_are_absent() -> None:
    failures: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "typing_extensions":
                failures.append(f"{path}: typing_extensions import")
            if isinstance(node, ast.Import) and any(
                name.name == "typing_extensions" for name in node.names
            ):
                failures.append(f"{path}: typing_extensions import")
            if isinstance(node, ast.Call):
                if (
                    isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "asyncio"
                    and node.func.attr in {"gather", "get_event_loop"}
                ):
                    failures.append(f"{path}:{node.lineno}: asyncio.{node.func.attr}")
                if any(
                    keyword.arg == "shell"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                    for keyword in node.keywords
                ):
                    failures.append(f"{path}:{node.lineno}: shell=True")
    assert failures == []


def test_openai_codex_imports_exist_only_in_sdk_package() -> None:
    failures: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports_sdk = any(
            (
                isinstance(node, ast.ImportFrom)
                and (node.module or "").startswith("openai_codex")
            )
            or (
                isinstance(node, ast.Import)
                and any(name.name.startswith("openai_codex") for name in node.names)
            )
            for node in ast.walk(tree)
        )
        if imports_sdk and path.parent != SOURCE / "sdk":
            failures.append(str(path))
    assert failures == []


def test_declared_python_runtime_is_exactly_314() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert 'requires-python = ">=3.14,<3.15"' in pyproject
    assert (
        "FROM python:3.14.6-alpine3.24@sha256:"
        "26730869004e2b9c4b9ad09cab8625e81d256d1ce97e72df5520e806b1709f92 AS base"
        in dockerfile
    )
    assert "FROM python:3.13" not in dockerfile
    assert "FROM python:3.12" not in dockerfile
