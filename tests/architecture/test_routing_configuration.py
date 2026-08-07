# SPDX-License-Identifier: MIT
# File: tests/architecture/test_routing_configuration.py
# Purpose: Enforce repository-neutral shipped routing configuration

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHIPPED_CONFIGS = (
    ROOT / "config/task-models.yaml",
    ROOT / "config/task-models.template.yaml",
)
REQUIRED_EXAMPLES = (
    "docker-invoke",
    "docker-cleanup",
    "python-coding",
    "rust-coding",
    "c-cpp-coding",
)


def test_shipped_configuration_is_repository_neutral() -> None:
    forbidden_names = ("huni", "sensai", "mcp-cpp")

    for path in SHIPPED_CONFIGS:
        content = path.read_text(encoding="utf-8")
        folded = content.casefold()
        assert all(name not in folded for name in forbidden_names)
        assert re.search(r"[a-zA-Z]:[\\/]", content) is None
        assert re.search(r"/mnt/[a-z]/", content) is None


def test_shipped_configuration_contains_required_generic_examples() -> None:
    for path in SHIPPED_CONFIGS:
        content = path.read_text(encoding="utf-8")
        assert all(example in content for example in REQUIRED_EXAMPLES)
