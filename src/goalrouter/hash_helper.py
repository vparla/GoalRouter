# SPDX-License-Identifier: MIT
# File: src/goalrouter/hash_helper.py
# Purpose: Killable raw Git-blob hashing for an inherited descriptor

"""Hash one already-open descriptor without resolving a project path."""

import hashlib
import os
import sys
from collections.abc import Sequence
from typing import Protocol

_READ_CHUNK_BYTES = 64 * 1024
_BATCH_MAX = 64


class _DigestProtocol(Protocol):
    def update(self, data: bytes) -> None: ...

    def hexdigest(self) -> str: ...


def _digest(algorithm: str) -> _DigestProtocol:
    if algorithm == "sha1":
        return hashlib.sha1(usedforsecurity=False)
    if algorithm == "sha256":
        return hashlib.sha256(usedforsecurity=False)
    raise ValueError("unsupported object format")


def main(arguments: Sequence[str] | None = None) -> int:
    values = tuple(arguments if arguments is not None else sys.argv[1:])
    if len(values) < 3 or len(values) % 2 == 0:
        return 2
    try:
        algorithm = values[0]
        requests = tuple(
            (int(values[index]), int(values[index + 1]))
            for index in range(1, len(values), 2)
        )
        if (
            len(requests) > _BATCH_MAX
            or len({descriptor for descriptor, _size in requests}) != len(requests)
            or any(descriptor < 0 or size < 0 for descriptor, size in requests)
        ):
            return 2
        output = bytearray()
        for descriptor, size in requests:
            digest = _digest(algorithm)
            digest.update(f"blob {size}\0".encode("ascii"))
            remaining = size
            while remaining:
                chunk = os.read(descriptor, min(remaining, _READ_CHUNK_BYTES))
                if not chunk:
                    return 3
                digest.update(chunk)
                remaining -= len(chunk)
            output.extend(digest.hexdigest().encode("ascii") + b"\n")
        written = 0
        while written < len(output):
            count = os.write(sys.stdout.fileno(), output[written:])
            if count <= 0:
                return 4
            written += count
    except (OSError, TypeError, ValueError):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
