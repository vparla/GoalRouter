# SPDX-License-Identifier: MIT
# File: tests/distribution/test_documented_commands.py
# Purpose: Validate safe public command examples in the declared container lifecycle

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from goalrouter.cli import build_parser

ROOT = Path(__file__).resolve().parents[2]

WINDOWS_INSTALL_COMMAND = r".\install.ps1 -Version $Version -Yes"
LINUX_INSTALL_COMMAND = "./install.sh --version 1.0.6 --yes"
MACOS_INSTALL_COMMAND = "./install.sh --version 1.0.6 --yes"


def _heading_block(content: str, heading: str) -> str:
    match = re.search(
        rf"(?ms)^##+ {re.escape(heading)}[^\n]*\n(.*?)(?=^##+ |\Z)",
        content,
    )
    assert match is not None, f"missing platform section: {heading}"
    fence = re.search(r"```(?:powershell|sh|text)\n(.*?)```", match.group(1), re.DOTALL)
    assert fence is not None, f"missing command block: {heading}"
    return fence.group(1).strip()


def _assert_ordered_lines(block: str, expected: tuple[str, ...]) -> None:
    lines = block.splitlines()
    positions: list[int] = []
    for command in expected:
        assert lines.count(command) == 1, f"expected one exact command: {command}"
        positions.append(lines.index(command))
    assert positions == sorted(positions), "documented lifecycle commands are out of order"


def _assert_install_contract(content: str) -> None:
    windows = _heading_block(content, "Windows")
    linux = _heading_block(content, "Linux")
    macos = _heading_block(content, "macOS")

    _assert_ordered_lines(
        windows,
        (
            'Invoke-WebRequest "$Release/SHA256SUMS" -OutFile .\\SHA256SUMS',
            'Invoke-WebRequest "$Release/install.ps1" -OutFile .\\install.ps1',
            'Invoke-WebRequest "$Release/goalrouter-$Version-windows.zip" '
            '-OutFile ".\\goalrouter-$Version-windows.zip"',
            "Get-Content .\\install.ps1",
            "$Checksums = Get-Content .\\SHA256SUMS",
            "foreach ($File in @('install.ps1', \"goalrouter-$Version-windows.zip\")) {",
            'Expand-Archive -Path ".\\goalrouter-$Version-windows.zip" '
            '-DestinationPath ".\\goalrouter-$Version" -WhatIf',
            WINDOWS_INSTALL_COMMAND,
        ),
    )
    _assert_ordered_lines(
        linux,
        (
            "curl --fail-with-body --location --proto '=https' --tlsv1.2 "
            '"$release/SHA256SUMS" -o SHA256SUMS',
            "curl --fail-with-body --location --proto '=https' --tlsv1.2 "
            '"$release/install.sh" -o install.sh',
            "grep ' install.sh$' SHA256SUMS > install.SHA256SUMS",
            "sha256sum -c install.SHA256SUMS",
            "sed -n '1,240p' install.sh",
            "curl --fail-with-body --location --proto '=https' --tlsv1.2 "
            '"$release/goalrouter-$version-unix.tar.gz" '
            '-o "goalrouter-$version-unix.tar.gz"',
            'grep " goalrouter-$version-unix.tar.gz$" SHA256SUMS > archive.SHA256SUMS',
            "sha256sum -c archive.SHA256SUMS",
            'tar -tzf "goalrouter-$version-unix.tar.gz"',
            "chmod 0700 install.sh",
            LINUX_INSTALL_COMMAND,
        ),
    )
    _assert_ordered_lines(
        macos,
        (
            "curl --fail-with-body --location --proto '=https' --tlsv1.2 "
            '"$release/SHA256SUMS" -o SHA256SUMS',
            "curl --fail-with-body --location --proto '=https' --tlsv1.2 "
            '"$release/install.sh" -o install.sh',
            "grep ' install.sh$' SHA256SUMS > install.SHA256SUMS",
            "shasum -a 256 -c install.SHA256SUMS",
            "sed -n '1,240p' install.sh",
            "curl --fail-with-body --location --proto '=https' --tlsv1.2 "
            '"$release/goalrouter-$version-unix.tar.gz" '
            '-o "goalrouter-$version-unix.tar.gz"',
            'grep " goalrouter-$version-unix.tar.gz$" SHA256SUMS > archive.SHA256SUMS',
            "shasum -a 256 -c archive.SHA256SUMS",
            'tar -tzf "goalrouter-$version-unix.tar.gz"',
            "chmod 0700 install.sh",
            MACOS_INSTALL_COMMAND,
        ),
    )
    assert "shasum" not in linux
    assert "sha256sum" not in macos


def _fenced_commands(content: str) -> set[str]:
    commands: set[str] = set()
    for block in re.findall(r"```(?:powershell|sh|text)\n(.*?)```", content, re.DOTALL):
        commands.update(line.strip() for line in block.splitlines() if line.strip())
    return commands

APPLICATION_INVOCATIONS = (
    ("config-template", ("config", "template")),
    ("config-validate", ("config", "validate")),
    ("version", ("version",)),
    ("models", ("models",)),
    (
        "route",
        (
            "route",
            "--project",
            "/project",
            "--task",
            "documentation",
            "--prompt",
            "Explain it",
            "--affected-path",
            "README.md",
        ),
    ),
    ("plan", ("plan", "--project", "/project", "--objective", "Plan it", "--run-id", "run-1")),
    (
        "run-task",
        (
            "run",
            "--project",
            "/project",
            "--task",
            "documentation",
            "--prompt",
            "Write it",
            "--run-id",
            "run-1",
        ),
    ),
    ("run-objective", ("run", "--project", "/project", "--objective", "Run it")),
    ("status", ("status", "run-1")),
    ("approve", ("approve", "run-1", "work-1", "--approved-by", "reviewer")),
    ("resume", ("resume", "run-1", "--acknowledge-configuration-change")),
    ("report", ("report", "run-1")),
)


@pytest.mark.parametrize(
    ("name", "arguments"),
    APPLICATION_INVOCATIONS,
    ids=[name for name, _arguments in APPLICATION_INVOCATIONS],
)
def test_every_documented_application_command_is_accepted_by_the_parser(
    name: str,
    arguments: tuple[str, ...],
) -> None:
    del name
    parsed = build_parser().parse_args(arguments)
    assert parsed.command == arguments[0]


@pytest.mark.parametrize(
    "arguments",
    [
        ("--help",),
        ("config", "--help"),
        ("route", "--help"),
        ("plan", "--help"),
        ("run", "--help"),
        ("status", "--help"),
        ("approve", "--help"),
        ("resume", "--help"),
        ("report", "--help"),
    ],
)
def test_documented_application_help_commands_execute_safely(
    arguments: tuple[str, ...],
) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "goalrouter", *arguments],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()
    assert result.stderr == ""


@pytest.mark.parametrize(
    "script",
    ["goalrouter", "install.sh", "uninstall.sh"],
)
def test_documented_posix_native_help_commands_execute_safely(script: str) -> None:
    result = subprocess.run(
        [str(ROOT / "scripts" / script), "--help"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("version-json", ("--json", "version")),
        ("config-validate-json", ("--json", "config", "validate")),
        ("config-template", ("config", "template")),
    ],
)
def test_documented_non_sdk_commands_execute_safely(
    name: str,
    arguments: tuple[str, ...],
) -> None:
    del name
    result = subprocess.run(
        [sys.executable, "-m", "goalrouter", *arguments],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout
    assert result.stderr == ""


def test_installation_and_quickstart_publish_copy_paste_host_commands() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    installation = (ROOT / "docs" / "installation.md").read_text(encoding="utf-8")
    quickstart = (ROOT / "docs" / "quickstart.md").read_text(encoding="utf-8")

    for content in (readme, installation):
        _assert_install_contract(content)
        for text in ("Get-FileHash", "Expand-Archive"):
            assert text in content

    for content in (readme, quickstart):
        for command in ("doctor", "config validate", "models", "route"):
            assert "goalrouter " in content and command in content
        assert "--access readonly" in content


def test_cross_platform_install_and_default_sso_workflow_are_documented() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    installation = (ROOT / "docs" / "installation.md").read_text(encoding="utf-8")
    authentication = (ROOT / "docs" / "authentication.md").read_text(encoding="utf-8")

    for content in (readme, installation):
        for heading in ("Windows", "Linux", "macOS"):
            assert f"### {heading}" in content or f"## {heading}:" in content
    normalized = " ".join(f"{readme}\n{installation}\n{authentication}".split())
    assert "application itself always runs in its pinned Python 3.14 container" in normalized
    assert "default `existing-session` mode" in normalized
    assert "does not require an API key" in normalized


def test_documented_lifecycle_commands_match_platform_grammar() -> None:
    readme = _fenced_commands((ROOT / "README.md").read_text(encoding="utf-8"))
    installation = _fenced_commands(
        (ROOT / "docs" / "installation.md").read_text(encoding="utf-8")
    )
    upgrading = _fenced_commands(
        (ROOT / "docs" / "upgrading.md").read_text(encoding="utf-8")
    )
    uninstalling = _fenced_commands(
        (ROOT / "docs" / "uninstalling.md").read_text(encoding="utf-8")
    )

    for commands in (readme, installation):
        assert {WINDOWS_INSTALL_COMMAND, LINUX_INSTALL_COMMAND} <= commands
        assert r".\install.ps1 -Version 1.0.6" not in commands
        assert "./install.sh --version 1.0.6" not in commands
    assert {"goalrouter update", "goalrouter update 1.0.6"} <= upgrading
    assert {
        "goalrouter uninstall",
        "goalrouter uninstall --yes",
        "goalrouter uninstall --purge --yes",
        "goalrouter uninstall -Yes",
        "goalrouter uninstall -Purge -Yes",
    } <= uninstalling

    windows_installer = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    windows_uninstaller = (ROOT / "scripts" / "uninstall.ps1").read_text(
        encoding="utf-8"
    )
    windows_launcher = (ROOT / "scripts" / "goalrouter.ps1").read_text(encoding="utf-8")
    posix_launcher = (ROOT / "scripts" / "goalrouter").read_text(encoding="utf-8")
    assert "installation requires -Yes" in windows_installer
    assert "uninstall requires -Yes" in windows_uninstaller
    for token in ("'-Purge'", "'-Yes'"):
        assert token in windows_launcher
    for token in ("--purge", "--yes"):
        assert token in posix_launcher


@pytest.mark.parametrize(
    ("mutation", "replacement"),
    [
        ("removed-windows-confirmation", (" -Yes", "")),
        ("removed-posix-confirmation", (" --yes", "")),
        ("collapsed-platform-block", ("### macOS", "### Linux")),
        (
            "checksum-token-drift",
            ("sha256sum -c archive.SHA256SUMS", "sha256sum -c install.SHA256SUMS"),
        ),
    ],
)
def test_install_contract_rejects_documentation_mutations(
    mutation: str, replacement: tuple[str, str]
) -> None:
    del mutation
    content = (ROOT / "README.md").read_text(encoding="utf-8")
    original, changed = replacement
    assert original in content
    with pytest.raises(AssertionError):
        _assert_install_contract(content.replace(original, changed, 1))


def test_contributor_examples_use_only_declared_compose_services() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    declared_services = {
        line.strip().removesuffix(":")
        for line in compose.splitlines()
        if line.startswith("  ") and not line.startswith("    ") and line.rstrip().endswith(":")
    }
    contributor_docs = (
        ROOT / "CONTRIBUTING.md",
        ROOT / "docs" / "development.md",
        ROOT / "docs" / "testing.md",
        ROOT / "docs" / "releasing.md",
    )
    observed_services: set[str] = set()
    for path in contributor_docs:
        content = path.read_text(encoding="utf-8")
        for line in content.splitlines():
            if "docker compose run --rm " not in line:
                continue
            suffix = line.split("docker compose run --rm ", 1)[1]
            service = suffix.split()[0].strip('"')
            observed_services.add(service)
            assert service in declared_services
    assert {"test", "lint", "typecheck", "package", "shellcheck", "powershell-test"} <= (
        observed_services
    )
