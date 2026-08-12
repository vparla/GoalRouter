# SPDX-License-Identifier: MIT
# File: tests/unit/test_build_info.py
# Purpose: Verify immutable build and protocol metadata

from goalrouter.build_info import current_build_info


def test_current_build_info_uses_stable_defaults_and_explicit_image_metadata() -> None:
    info = current_build_info(
        {
            "GOALROUTER_IMAGE_REFERENCE": "ghcr.io/vparla/goalrouter@sha256:abc",
            "GOALROUTER_IMAGE_REVISION": "0123456789abcdef",
        }
    )

    assert info.version == "1.0.4"
    assert info.protocol_version == 1
    assert info.image_reference is not None
    assert info.image_reference.endswith("@sha256:abc")
    assert info.image_revision == "0123456789abcdef"
