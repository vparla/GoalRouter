# SPDX-License-Identifier: MIT
# File: src/goalrouter/build_info.py
# Purpose: Immutable GoalRouter build and protocol metadata

"""Build metadata exposed to launchers and distribution tooling."""

from collections.abc import Mapping
from dataclasses import dataclass

from goalrouter import __version__
from goalrouter.domain import JsonValue


@dataclass(frozen=True, slots=True)
class BuildInfo:
    """Immutable metadata for a GoalRouter image or package."""

    version: str
    protocol_version: int
    image_reference: str | None
    image_revision: str | None

    def to_json_value(self) -> dict[str, JsonValue]:
        """Return the stable JSON representation consumed by launchers."""

        return {
            "version": self.version,
            "protocol_version": self.protocol_version,
            "image_reference": self.image_reference,
            "image_revision": self.image_revision,
        }


def current_build_info(environ: Mapping[str, str]) -> BuildInfo:
    """Return static package metadata with explicit image provenance when supplied."""

    return BuildInfo(
        version=__version__,
        protocol_version=1,
        image_reference=environ.get("GOALROUTER_IMAGE_REFERENCE"),
        image_revision=environ.get("GOALROUTER_IMAGE_REVISION"),
    )
