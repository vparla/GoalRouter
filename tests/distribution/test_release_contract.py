# SPDX-License-Identifier: MIT
# File: tests/distribution/test_release_contract.py
# Purpose: Enforce cross-platform release and install-manifest parity

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POSIX_INSTALLER = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
WINDOWS_INSTALLER = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
WINDOWS_LAUNCHER = (ROOT / "scripts" / "goalrouter.ps1").read_text(encoding="utf-8")
PUBLIC_CONTRACT = json.loads(
    (ROOT / "tests" / "fixtures" / "distribution" / "public-launcher-contract.json").read_text(
        encoding="utf-8"
    )
)
SHARED_RELEASE_MANIFEST = (
    ROOT / "tests" / "fixtures" / "distribution" / "release-manifest.json"
).read_text(encoding="ascii").strip()


RELEASE_FIELDS = {
    "version",
    "protocol_version",
    "image",
    "image_digest",
    "architectures",
    "source_revision",
    "minimum_hosts",
}
INSTALL_COMMON_FIELDS = {
    "manifest_version",
    "protocol_version",
    "version",
    "launcher_version",
    "image_reference",
    "image_digest",
    "image_platform",
    "source_revision",
    "owned",
}


def _powershell_exact_names(label: str) -> set[str]:
    match = re.search(
        rf"Assert-GoalRouterExactProperties -Value \${label} -Names @\(([^)]*)\)",
        WINDOWS_INSTALLER,
    )
    assert match is not None
    return set(re.findall(r"'([a-z_]+)'", match.group(1)))


def _powershell_ordered_manifest_fields() -> set[str]:
    match = re.search(
        r"function New-GoalRouterInstallManifest \{.*?return \[ordered\]@\{(.*?)\n    \}",
        WINDOWS_INSTALLER,
        flags=re.DOTALL,
    )
    assert match is not None
    return set(re.findall(r"^        ([a-z_]+) =", match.group(1), flags=re.MULTILINE))


def _posix_install_manifest_fields() -> set[str]:
    line = next(
        line for line in POSIX_INSTALLER.splitlines() if line.startswith('{"manifest_version"')
    )
    return set(re.findall(r'"([a-z_]+)":', line))


def test_release_manifest_common_fields_and_platform_minimums_are_exact() -> None:
    assert _powershell_exact_names("manifest") == RELEASE_FIELDS
    for field in RELEASE_FIELDS:
        assert f'"{field}"' in POSIX_INSTALLER
    assert _powershell_exact_names("manifest.minimum_hosts") == {
        "windows",
        "powershell",
        "wsl",
        "docker",
    }
    assert (
        '"minimum_hosts":{"windows":"%s","powershell":"%s","wsl":"%s",'
        '"docker":"%s"}'
    ) in POSIX_INSTALLER


def test_one_serialized_release_manifest_schema_is_consumed_by_both_installers() -> None:
    payload = json.loads(SHARED_RELEASE_MANIFEST)
    assert set(payload) == RELEASE_FIELDS
    assert set(payload["minimum_hosts"]) == {"windows", "powershell", "wsl", "docker"}
    for key in payload["minimum_hosts"]:
        assert f'manifest_minimum_{key}' in POSIX_INSTALLER
        assert f"manifest.minimum_hosts.{key}" in WINDOWS_INSTALLER


def test_install_manifests_share_task6_fields_and_windows_adds_only_host_control() -> None:
    posix_fields = _posix_install_manifest_fields()
    windows_fields = _powershell_ordered_manifest_fields()
    assert posix_fields >= INSTALL_COMMON_FIELDS
    assert windows_fields >= INSTALL_COMMON_FIELDS
    assert windows_fields - INSTALL_COMMON_FIELDS == {
        "wsl_distribution",
        "path_ownership",
        "release_base",
    }


def test_protocol_version_image_digest_and_revision_bind_both_installers() -> None:
    for source in (POSIX_INSTALLER, WINDOWS_INSTALLER):
        assert "protocol_version" in source
        assert "image_digest" in source
        assert "source_revision" in source
        assert "RepoDigest" in source or "repo_digest" in source
    assert "candidate image digest does not match trusted release manifest" in WINDOWS_INSTALLER
    assert "candidate image revision does not match trusted release manifest" in WINDOWS_INSTALLER


def test_maintenance_names_and_exact_cmd_shim_remain_shared() -> None:
    assert PUBLIC_CONTRACT["maintenance_commands"] == ["doctor", "update", "version", "uninstall"]
    posix_launcher = (ROOT / "scripts" / "goalrouter").read_text(encoding="utf-8")
    for command in PUBLIC_CONTRACT["maintenance_commands"]:
        assert re.search(rf"\b{command}\b", WINDOWS_LAUNCHER)
        assert re.search(rf"\b{command}\b", posix_launcher)
    assert (ROOT / "scripts" / "goalrouter.cmd").read_bytes() == (
        b"@echo off\n"
        b"powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass "
        b'-File "%~dp0goalrouter.ps1" %*\n'
        b"exit /b %ERRORLEVEL%\n"
    )


def test_windows_lifecycle_metadata_excludes_credentials_and_temporary_paths() -> None:
    manifest_tail = WINDOWS_INSTALLER.split("function New-GoalRouterInstallManifest", 1)[1]
    manifest_function = manifest_tail.split("function Get-GoalRouterImageRepository", 1)[0]
    for forbidden in ("OPENAI_API_KEY", "Bearer ", "password", "credential", "workDirectory"):
        assert forbidden not in manifest_function
