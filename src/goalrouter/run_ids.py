# SPDX-License-Identifier: MIT
# File: src/goalrouter/run_ids.py
# Purpose: Shared validation for persisted run identifiers and lease names

"""Validate run identifiers before deriving any filesystem path."""

import re

from goalrouter.errors import StateError

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_run_id(run_id: str) -> str:
    """Return a safe run identifier or fail before filesystem access."""

    if _RUN_ID.fullmatch(run_id) is None:
        raise StateError(f"Invalid run ID {run_id!r}")
    return run_id
