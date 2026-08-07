# SPDX-License-Identifier: MIT
# File: tests/architecture/test_routing_policy.py
# Purpose: Enforce repository-neutral and data-driven routing policy

import ast
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATHS = (
    ROOT / "config/task-models.yaml",
    ROOT / "config/task-models.template.yaml",
)
EXAMPLE_TASKS = {
    "objective-planning",
    "completion-review",
    "repository-search",
    "docker-invoke",
    "docker-cleanup",
    "unit-test-run",
    "unit-test-debug",
    "bounded-diagnosis",
    "documentation",
    "configuration-edit",
    "python-coding",
    "c-cpp-coding",
    "rust-coding",
    "core-debugging",
    "architecture-change",
    "security-sensitive",
    "release-publish",
}


def _config(path: Path) -> dict[str, object]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_router_contains_no_configured_task_or_model_literals() -> None:
    config = _config(CONFIG_PATHS[0])
    tasks = config["tasks"]
    aliases = config["model-aliases"]
    assert isinstance(tasks, dict)
    assert isinstance(aliases, dict)
    forbidden = set(tasks)
    forbidden.update(
        value["model"] for value in aliases.values() if isinstance(value, dict)
    )
    tree = ast.parse(
        (ROOT / "src/goalrouter/routing.py").read_text(encoding="utf-8")
    )
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert forbidden.isdisjoint(literals)


def test_shipped_yaml_has_complete_examples_and_no_repository_identity() -> None:
    for path in CONFIG_PATHS:
        raw = path.read_text(encoding="utf-8")
        config = _config(path)
        tasks = config["tasks"]
        assert isinstance(tasks, dict)
        assert set(tasks) >= EXAMPLE_TASKS
        folded = raw.casefold()
        for identity in ("huni", "sensai", "mcp-cpp", "c:\\dev", "/projects/"):
            assert identity not in folded
