# SPDX-License-Identifier: MIT
# File: tests/distribution/test_installation_roundtrip.py
# Purpose: Verify the generated local distribution round-trip gate

from pathlib import Path

import yaml


def test_roundtrip_gate_uses_generated_assets_and_owned_cleanup() -> None:
    compose = yaml.safe_load(Path("compose.yaml").read_text(encoding="utf-8"))
    service = compose["services"]["distribution-integration"]
    command = service["command"]

    assert command == [
        "python",
        "-m",
        "pytest",
        "tests/distribution/test_launcher_integration.py",
        "tests/distribution/test_authority_profiles.py",
        "tests/distribution/test_installation_roundtrip.py",
        "-q",
    ]


def test_posix_installer_smoke_covers_install_update_and_uninstall_roundtrip() -> None:
    harness = Path("scripts/posix-installer-smoke.sh").read_text(encoding="utf-8")

    assert '"$HOME/.local/bin/goalrouter-install"' in harness
    assert harness.count("--version 1.0.7") >= 2
    assert "goalrouter-1.0.7-unix.tar.gz" in harness
    assert "configuration-before-update" in harness
    assert "state-before-update" in harness
    assert "no owned Docker resources remain" in harness


def test_windows_and_posix_installed_surfaces_share_stable_contracts() -> None:
    posix = Path("scripts/goalrouter").read_text(encoding="utf-8")
    powershell = Path("scripts/goalrouter.ps1").read_text(encoding="utf-8")
    install_posix = Path("scripts/install.sh").read_text(encoding="utf-8")
    install_powershell = Path("scripts/install.ps1").read_text(encoding="utf-8")

    for command in ("doctor", "update", "version", "uninstall"):
        assert command in posix
        assert command in powershell
    for field in (
        "protocol_version",
        "image_reference",
        "image_digest",
        "source_revision",
    ):
        assert field in install_posix.replace("-", "_")
        assert field in install_powershell
