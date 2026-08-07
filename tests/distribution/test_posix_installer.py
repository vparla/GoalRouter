# SPDX-License-Identifier: MIT
# File: tests/distribution/test_posix_installer.py
# Purpose: Verify the POSIX installation, maintenance, and removal lifecycle

from __future__ import annotations

import hashlib
import http.server
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import tarfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

INSTALLER: Final = Path("scripts/install.sh").resolve()
UNINSTALLER: Final = Path("scripts/uninstall.sh").resolve()
LAUNCHER: Final = Path("scripts/goalrouter").resolve()
FAKE_DOCKER: Final = Path(
    "tests/fixtures/distribution/fake-release/docker"
).resolve()
DIGEST: Final = "sha256:" + ("a" * 64)


@dataclass(frozen=True, slots=True)
class InstallHome:
    root: Path
    home: Path
    release: Path
    fake_bin: Path
    docker_log: Path

    @property
    def bin_dir(self) -> Path:
        return self.home / ".local" / "bin"

    @property
    def config_dir(self) -> Path:
        return self.home / ".config" / "goalrouter"

    @property
    def state_dir(self) -> Path:
        return self.home / ".local" / "state" / "goalrouter"

    @property
    def launcher(self) -> Path:
        return self.bin_dir / "goalrouter"

    @property
    def installed_installer(self) -> Path:
        return self.bin_dir / "goalrouter-install"

    @property
    def installed_uninstaller(self) -> Path:
        return self.bin_dir / "goalrouter-uninstall"

    @property
    def config(self) -> Path:
        return self.config_dir / "task-models.yaml"

    @property
    def control_dir(self) -> Path:
        return self.bin_dir / ".goalrouter-control"

    def environment(self, **overrides: str) -> dict[str, str]:
        environment = {
            "HOME": str(self.home),
            "PATH": f"{self.fake_bin}:/usr/local/bin:/usr/bin:/bin",
            "FAKE_DOCKER_LOG": str(self.docker_log),
        }
        environment.update(overrides)
        return environment


def _snapshot_tree(root: Path) -> dict[str, tuple[str, int, bytes]]:
    snapshot: dict[str, tuple[str, int, bytes]] = {}
    root_mode = stat.S_IMODE(root.lstat().st_mode)
    snapshot["."] = ("directory", root_mode, b"")
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in sorted([*directory_names, *file_names]):
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            path_stat = path.lstat()
            mode = stat.S_IMODE(path_stat.st_mode)
            if stat.S_ISLNK(path_stat.st_mode):
                snapshot[relative] = ("link", mode, os.fsencode(os.readlink(path)))
            elif stat.S_ISDIR(path_stat.st_mode):
                snapshot[relative] = ("directory", mode, b"")
            elif stat.S_ISREG(path_stat.st_mode):
                snapshot[relative] = ("file", mode, path.read_bytes())
            else:
                snapshot[relative] = ("other", mode, b"")
    return snapshot


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        del format, args


@contextmanager
def serve_release(directory: Path) -> Iterator[str]:
    handler = lambda *args, **kwargs: _QuietHandler(  # noqa: E731
        *args, directory=str(directory), **kwargs
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _member(name: str, data: bytes, *, mode: int = 0o755) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = 0
    return info, data


def write_release(
    release: Path,
    version: str,
    *,
    members: list[tuple[tarfile.TarInfo, bytes]] | None = None,
    checksum_lines: list[str] | None = None,
) -> None:
    release.mkdir(parents=True, exist_ok=True)
    archive = release / f"goalrouter-{version}-unix.tar.gz"
    if members is None:
        installer = INSTALLER.read_bytes() if INSTALLER.exists() else b"#!/bin/sh\nexit 0\n"
        uninstaller = (
            UNINSTALLER.read_bytes() if UNINSTALLER.exists() else b"#!/bin/sh\nexit 0\n"
        )
        members = [
            _member("goalrouter", LAUNCHER.read_bytes()),
            _member("install.sh", installer),
            _member("uninstall.sh", uninstaller),
        ]
    with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as package:
        for info, data in members:
            package.addfile(info, io.BytesIO(data) if info.isreg() else None)
    manifest = release / "release-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": version,
                "protocol_version": 1,
                "image": f"registry.example/goalrouter:{version}",
                "image_digest": DIGEST,
                "architectures": ["linux/amd64", "linux/arm64"],
                "source_revision": "fixture-revision",
                "minimum_hosts": {
                    "windows": "10.0.19045",
                    "powershell": "5.1",
                    "wsl": "2.2.3",
                    "docker": "20.10",
                },
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="ascii",
    )
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest_digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    lines = checksum_lines or [
        f"{manifest_digest}  {manifest.name}",
        f"{digest}  {archive.name}",
    ]
    (release / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="ascii")


def rewrite_manifest_minimum(release: Path, key: str, minimum: str) -> None:
    manifest = release / "release-manifest.json"
    payload = json.loads(manifest.read_text(encoding="ascii"))
    payload["minimum_hosts"][key] = minimum
    manifest.write_text(
        json.dumps(payload, separators=(",", ":")) + "\n", encoding="ascii"
    )
    assets = [manifest, *sorted(release.glob("goalrouter-*-unix.tar.gz"))]
    lines = [
        f"{hashlib.sha256(asset.read_bytes()).hexdigest()}  {asset.name}"
        for asset in assets
    ]
    (release / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="ascii")


def rewrite_manifest_minimum_docker(release: Path, minimum: str) -> None:
    rewrite_manifest_minimum(release, "docker", minimum)


@pytest.fixture
def install_home(tmp_path: Path) -> InstallHome:
    home = tmp_path / "home"
    release = tmp_path / "release"
    fake_bin = tmp_path / "fake-bin"
    home.mkdir()
    fake_bin.mkdir()
    (fake_bin / "docker").symlink_to(FAKE_DOCKER)
    uname = fake_bin / "uname"
    uname.write_text('#!/bin/sh\nprintf "%s\\n" "${FAKE_UNAME:-x86_64}"\n', encoding="utf-8")
    uname.chmod(0o755)
    write_release(release, "1.0.0")
    return InstallHome(tmp_path, home, release, fake_bin, tmp_path / "docker.log")


def run_installer(
    fixture: InstallHome,
    base: str,
    *arguments: str,
    environment: dict[str, str] | None = None,
    skip_doctor: bool = True,
) -> subprocess.CompletedProcess[str]:
    active_environment = fixture.environment()
    if environment:
        active_environment.update(environment)
    command = [
        str(INSTALLER),
        "--version",
        "1.0.0",
        "--release-base",
        base,
        "--allow-loopback-http",
        "--image",
        "registry.example/goalrouter:1.0.0",
        "--yes",
        *arguments,
    ]
    if skip_doctor:
        command.append("--skip-doctor")
    return subprocess.run(
        command,
        env=active_environment,
        capture_output=True,
        check=False,
        text=True,
    )


def install(fixture: InstallHome, **environment: str) -> subprocess.CompletedProcess[str]:
    with serve_release(fixture.release) as base:
        return run_installer(fixture, base, environment=environment)


def assert_no_installation(fixture: InstallHome) -> None:
    assert not fixture.launcher.exists()
    assert not (fixture.state_dir / "install.json").exists()
    assert not fixture.config.exists()


def test_clean_loopback_install_records_immutable_owned_manifest(
    install_home: InstallHome,
) -> None:
    result = install(install_home, OPENAI_API_KEY="must-not-be-recorded")

    assert result.returncode == 0, result.stderr
    assert install_home.launcher.stat().st_mode & 0o777 == 0o755
    assert install_home.installed_installer.stat().st_mode & 0o777 == 0o755
    assert install_home.installed_uninstaller.stat().st_mode & 0o777 == 0o755
    assert install_home.config_dir.stat().st_mode & 0o777 == 0o700
    assert install_home.state_dir.stat().st_mode & 0o777 == 0o700
    assert install_home.control_dir.stat().st_mode & 0o777 == 0o700
    assert install_home.config.read_text(encoding="utf-8").startswith("schema-version: 1")
    assert (install_home.state_dir / "image-ref").read_text(encoding="utf-8").strip() == (
        "registry.example/goalrouter"
    )
    assert (install_home.state_dir / "image-digest").read_text(encoding="utf-8").strip() == DIGEST
    metadata = json.loads(
        (install_home.state_dir / "install.json").read_text(encoding="utf-8")
    )
    assert metadata["manifest_version"] == 1
    assert metadata["protocol_version"] == 1
    assert metadata["version"] == "1.0.0"


def test_candidate_config_validation_uses_invoking_numeric_identity(
    install_home: InstallHome,
) -> None:
    result = install(install_home)

    assert result.returncode == 0, result.stderr
    validation_commands = [
        line
        for line in install_home.docker_log.read_text(encoding="utf-8").splitlines()
        if " config validate" in line
    ]
    assert validation_commands
    expected_identity = f"--user {os.getuid()}:{os.getgid()}"
    assert all(expected_identity in command for command in validation_commands)
    all_metadata = b"".join(path.read_bytes() for path in install_home.state_dir.iterdir())
    assert b"must-not-be-recorded" not in all_metadata
    assert b"OPENAI_API_KEY" not in all_metadata


def test_public_download_uses_https_only_curl_flags(install_home: InstallHome) -> None:
    curl_log = install_home.root / "curl.log"
    fake_curl = install_home.fake_bin / "curl"
    fake_curl.write_text(
        "#!/bin/sh\nprintf '%s\\0' \"$@\" >\"$FAKE_CURL_LOG\"\nexit 22\n",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)

    result = subprocess.run(
        [
            str(INSTALLER),
            "--version",
            "1.0.0",
            "--release-base",
            "https://example.invalid/releases",
            "--image",
            "registry.example/goalrouter:1.0.0",
            "--yes",
            "--skip-doctor",
        ],
        env=install_home.environment(FAKE_CURL_LOG=str(curl_log)),
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    argv = curl_log.read_bytes().split(b"\0")[:-1]
    assert b"--fail" in argv
    assert b"--location" in argv
    proto_index = argv.index(b"--proto")
    assert argv[proto_index : proto_index + 2] == [b"--proto", b"=https"]
    redirect_proto_index = argv.index(b"--proto-redir")
    assert argv[redirect_proto_index : redirect_proto_index + 2] == [
        b"--proto-redir",
        b"=https",
    ]
    assert b"--tlsv1.2" in argv
    assert_no_installation(install_home)


def test_http_requires_explicit_loopback_only_opt_in(install_home: InstallHome) -> None:
    for url, extra in (
        ("http://127.0.0.1:9999", []),
        ("http://example.com", ["--allow-loopback-http"]),
    ):
        result = subprocess.run(
            [
                str(INSTALLER),
                "--version",
                "1.0.0",
                "--release-base",
                url,
                "--image",
                "registry.example/goalrouter:1.0.0",
                "--yes",
                "--skip-doctor",
                *extra,
            ],
            env=install_home.environment(),
            capture_output=True,
            check=False,
            text=True,
        )
        assert result.returncode != 0
        assert_no_installation(install_home)


def test_loopback_http_rejects_userinfo_authority_confusion_before_curl(
    install_home: InstallHome,
) -> None:
    curl_marker = install_home.root / "curl-invoked"
    fake_curl = install_home.fake_bin / "curl"
    fake_curl.write_text(
        f"#!/bin/sh\nprintf invoked >'{curl_marker}'\nexit 99\n", encoding="utf-8"
    )
    fake_curl.chmod(0o755)

    result = run_installer(
        install_home,
        "http://127.0.0.1:80@example.com/releases",
        "--allow-loopback-http",
    )

    assert result.returncode != 0
    assert not curl_marker.exists()
    assert_no_installation(install_home)


@pytest.mark.parametrize(
    "release_base",
    [
        "https://user:top-secret@example.invalid/releases",
        "https://@example.invalid/releases",
        "https:///missing-authority",
        "https://example..invalid/releases",
        "https://-example.invalid/releases",
        "https://example.invalid:/releases",
        "https://example.invalid/releases?token=top-secret",
        "https://example.invalid/releases#top-secret",
    ],
)
def test_https_release_authority_ambiguity_is_rejected_without_secret_output(
    install_home: InstallHome, release_base: str
) -> None:
    curl_marker = install_home.root / "curl-invoked"
    fake_curl = install_home.fake_bin / "curl"
    fake_curl.write_text(
        f"#!/bin/sh\nprintf invoked >'{curl_marker}'\nexit 99\n", encoding="utf-8"
    )
    fake_curl.chmod(0o755)

    result = subprocess.run(
        [
            str(INSTALLER),
            "--version",
            "1.0.0",
            "--release-base",
            release_base,
            "--image",
            "registry.example/goalrouter:1.0.0",
            "--yes",
            "--skip-doctor",
        ],
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert not curl_marker.exists()
    assert "top-secret" not in result.stderr
    assert release_base not in result.stderr
    assert_no_installation(install_home)


def test_https_download_failure_does_not_echo_custom_release_url(
    install_home: InstallHome,
) -> None:
    fake_curl = install_home.fake_bin / "curl"
    fake_curl.write_text("#!/bin/sh\nexit 22\n", encoding="utf-8")
    fake_curl.chmod(0o755)
    release_base = "https://downloads.example.invalid/private-release-name"

    result = subprocess.run(
        [
            str(INSTALLER),
            "--version",
            "1.0.0",
            "--release-base",
            release_base,
            "--image",
            "registry.example/goalrouter:1.0.0",
            "--yes",
            "--skip-doctor",
        ],
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert release_base not in result.stderr
    assert "private-release-name" not in result.stderr
    assert_no_installation(install_home)


@pytest.mark.parametrize("kind", ["missing", "duplicate", "malformed", "mismatch"])
def test_checksum_entry_must_be_exactly_one_and_match(
    install_home: InstallHome, kind: str
) -> None:
    archive_name = "goalrouter-1.0.0-unix.tar.gz"
    archive = install_home.release / archive_name
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = install_home.release / "release-manifest.json"
    manifest_digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    archive_lines = {
        "missing": [f"{digest}  another.tar.gz"],
        "duplicate": [f"{digest}  {archive_name}", f"{digest} *{archive_name}"],
        "malformed": [f"not-a-digest  {archive_name}"],
        "mismatch": [("0" * 64) + f"  {archive_name}"],
    }[kind]
    lines = [f"{manifest_digest}  {manifest.name}", *archive_lines]
    (install_home.release / "SHA256SUMS").write_text(
        "\n".join(lines) + "\n", encoding="ascii"
    )

    result = install(install_home)

    assert result.returncode != 0
    if kind == "mismatch":
        assert "downloaded archive checksum mismatch" in result.stderr
    else:
        assert "exactly one valid asset entry" in result.stderr
    assert_no_installation(install_home)


@pytest.mark.parametrize(
    "kind", ["traversal", "absolute", "unexpected", "duplicate", "link", "mode"]
)
def test_hostile_archive_is_rejected_before_extraction(
    install_home: InstallHome, kind: str
) -> None:
    valid = [
        _member("goalrouter", LAUNCHER.read_bytes()),
        _member("install.sh", b"#!/bin/sh\nexit 0\n"),
        _member("uninstall.sh", b"#!/bin/sh\nexit 0\n"),
    ]
    if kind == "traversal":
        valid[2] = _member("../uninstall.sh", b"bad")
    elif kind == "absolute":
        valid[2] = _member("/uninstall.sh", b"bad")
    elif kind == "unexpected":
        valid.append(_member("README", b"bad"))
    elif kind == "duplicate":
        valid.append(_member("goalrouter", b"bad"))
    elif kind == "link":
        link = tarfile.TarInfo("uninstall.sh")
        link.type = tarfile.SYMTYPE
        link.linkname = "/tmp/escape"
        link.mode = 0o777
        valid[2] = (link, b"")
    elif kind == "mode":
        valid[2] = _member("uninstall.sh", b"bad", mode=0o4755)
    write_release(install_home.release, "1.0.0", members=valid)

    result = install(install_home)

    assert result.returncode != 0
    assert_no_installation(install_home)
    assert not (install_home.root / "uninstall.sh").exists()


@pytest.mark.parametrize(
    ("architecture", "expected_platform", "expected_success"),
    [
        ("x86_64", "linux/amd64", True),
        ("aarch64", "linux/arm64", True),
        ("arm64", "linux/arm64", True),
        ("i686", "", False),
    ],
)
def test_architecture_is_mapped_or_rejected_before_mutation(
    install_home: InstallHome,
    architecture: str,
    expected_platform: str,
    expected_success: bool,
) -> None:
    result = install(
        install_home,
        FAKE_UNAME=architecture,
        FAKE_DOCKER_ARCH=(expected_platform.split("/")[-1] or "amd64"),
    )

    assert (result.returncode == 0) is expected_success
    if expected_success:
        installed_platform = (install_home.state_dir / "image-platform").read_text(
            encoding="utf-8"
        )
        assert installed_platform.strip() == expected_platform
    else:
        assert_no_installation(install_home)


def test_prerequisite_failure_occurs_before_product_mutation(install_home: InstallHome) -> None:
    result = install(install_home, FAKE_DOCKER_DAEMON_FAILURE="1")

    assert result.returncode != 0
    assert_no_installation(install_home)


def test_foreign_owned_destination_is_rejected_before_download(
    install_home: InstallHome,
) -> None:
    foreign_parent = install_home.root / "foreign-owned"
    foreign_parent.mkdir()
    os.chown(foreign_parent, 12345, 12345)

    with serve_release(install_home.release) as base:
        result = run_installer(
            install_home,
            base,
            "--bin-dir",
            str(foreign_parent / "bin"),
        )

    assert result.returncode != 0
    assert not (foreign_parent / "bin").exists()
    assert not (install_home.state_dir / "install.json").exists()


@pytest.mark.parametrize("target", ["config", "state"])
def test_preexisting_nonempty_unowned_destination_is_rejected(
    install_home: InstallHome, target: str
) -> None:
    destination = install_home.config_dir if target == "config" else install_home.state_dir
    destination.mkdir(parents=True)
    (destination / "unrelated.txt").write_text("preserve", encoding="utf-8")

    result = install(install_home)

    assert result.returncode != 0
    assert (destination / "unrelated.txt").read_text(encoding="utf-8") == "preserve"
    assert not install_home.launcher.exists()


def test_preexisting_configuration_only_is_preserved_and_adopted(
    install_home: InstallHome,
) -> None:
    install_home.config_dir.mkdir(parents=True)
    install_home.config.write_text(
        "schema-version: 1\ncustom: preserved\n", encoding="utf-8"
    )

    result = install(install_home)

    assert result.returncode == 0, result.stderr
    assert "custom: preserved" in install_home.config.read_text(encoding="utf-8")
    assert (install_home.config_dir / ".goalrouter-owned-v1").is_file()
    assert (install_home.state_dir / ".goalrouter-owned-v1").is_file()


@pytest.mark.parametrize(
    "environment",
    [
        {"FAKE_DOCKER_ARCH": "arm64"},
        {"FAKE_DOCKER_PROTOCOL": "2"},
        {"FAKE_DOCKER_APP_VERSION": "9.9.9"},
        {"FAKE_DOCKER_TEMPLATE_FAILURE": "1"},
        {"FAKE_DOCKER_TEMPLATE_VALIDATION_FAILURE": "1"},
    ],
)
def test_candidate_compatibility_failure_precedes_product_mutation(
    install_home: InstallHome, environment: dict[str, str]
) -> None:
    result = install(install_home, **environment)

    assert result.returncode != 0
    assert_no_installation(install_home)


def test_release_manifest_digest_mismatch_rejects_tag_before_candidate_execution(
    install_home: InstallHome,
) -> None:
    result = install(install_home, FAKE_DOCKER_DIGEST="sha256:" + ("b" * 64))

    assert result.returncode != 0
    docker_commands = install_home.docker_log.read_text(encoding="utf-8").splitlines()
    assert not any(command.startswith("run ") for command in docker_commands)
    assert_no_installation(install_home)


@pytest.mark.parametrize("minimum", ["999.0", "not-a-version"])
def test_release_manifest_minimum_docker_is_enforced_before_pull_or_mutation(
    install_home: InstallHome, minimum: str
) -> None:
    rewrite_manifest_minimum_docker(install_home.release, minimum)

    result = install(install_home)

    assert result.returncode != 0
    docker_commands = install_home.docker_log.read_text(encoding="utf-8").splitlines()
    assert not any(command.startswith(("pull ", "run ")) for command in docker_commands)
    assert_no_installation(install_home)


@pytest.mark.parametrize("key", ["windows", "powershell", "wsl", "docker"])
@pytest.mark.parametrize("minimum", ["2.bad", "2.", ".2"])
def test_release_manifest_minimum_versions_require_numeric_components(
    install_home: InstallHome, key: str, minimum: str
) -> None:
    rewrite_manifest_minimum(install_home.release, key, minimum)

    result = install(install_home)

    assert result.returncode != 0
    docker_commands = install_home.docker_log.read_text(encoding="utf-8").splitlines()
    assert not any(command.startswith(("pull ", "run ")) for command in docker_commands)
    assert_no_installation(install_home)


def test_reinstall_is_idempotent_and_reset_is_explicit(install_home: InstallHome) -> None:
    first = install(install_home)
    assert first.returncode == 0, first.stderr
    install_home.config.write_text("schema-version: 1\ncustom: preserve\n", encoding="utf-8")

    second = install(install_home)
    assert second.returncode == 0, second.stderr
    assert "custom: preserve" in install_home.config.read_text(encoding="utf-8")

    with serve_release(install_home.release) as base:
        reset = run_installer(install_home, base, "--reset-config")
    assert reset.returncode == 0, reset.stderr
    assert "custom: preserve" not in install_home.config.read_text(encoding="utf-8")


def test_corrupt_existing_metadata_requires_force_before_repair(
    install_home: InstallHome,
) -> None:
    first = install(install_home)
    assert first.returncode == 0, first.stderr
    (install_home.state_dir / "install.json").write_text("corrupt\n", encoding="utf-8")

    rejected = install(install_home)
    assert rejected.returncode != 0
    assert (install_home.state_dir / "install.json").read_text(encoding="utf-8") == (
        "corrupt\n"
    )

    with serve_release(install_home.release) as base:
        repaired = run_installer(install_home, base, "--force")
    assert repaired.returncode == 0, repaired.stderr
    repaired_metadata = json.loads(
        (install_home.state_dir / "install.json").read_text(encoding="utf-8")
    )
    assert repaired_metadata["manifest_version"] == 1


def test_failed_post_switch_doctor_rolls_back_every_owned_file(
    install_home: InstallHome,
) -> None:
    with serve_release(install_home.release) as base:
        result = run_installer(install_home, base, skip_doctor=False)

    assert result.returncode != 0
    assert_no_installation(install_home)
    assert not install_home.installed_installer.exists()
    assert not install_home.installed_uninstaller.exists()


def test_paths_with_spaces_and_metacharacters_work_but_newline_is_rejected(
    install_home: InstallHome,
) -> None:
    custom_root = install_home.root / "locations with spaces ;$[]!"
    bin_dir = custom_root / "bin"
    config_dir = custom_root / "config"
    state_dir = custom_root / "state"
    with serve_release(install_home.release) as base:
        result = run_installer(
            install_home,
            base,
            "--bin-dir",
            str(bin_dir),
            "--config-dir",
            str(config_dir),
            "--state-dir",
            str(state_dir),
        )
    assert result.returncode == 0, result.stderr
    assert (bin_dir / "goalrouter").exists()
    assert (config_dir / "task-models.yaml").exists()
    assert (state_dir / "install.json").exists()

    bad_state = install_home.root / "bad\nstate"
    with serve_release(install_home.release) as base:
        rejected = run_installer(install_home, base, "--state-dir", str(bad_state))
    assert rejected.returncode != 0
    assert not bad_state.exists()


def test_update_rejects_candidate_config_and_keeps_previous_install(
    install_home: InstallHome,
) -> None:
    write_release(install_home.release, "1.0.0")

    with serve_release(install_home.release) as base:
        first = run_installer(install_home, base)
        assert first.returncode == 0, first.stderr
        install_home.config.write_text(
            "schema-version: 1\ncustom: rollback-preserve\n", encoding="utf-8"
        )
        durable = install_home.state_dir / "runs" / "rollback-preserve"
        durable.parent.mkdir()
        durable.write_text("durable-state", encoding="utf-8")
        before = {
            "bin": _snapshot_tree(install_home.bin_dir),
            "config": _snapshot_tree(install_home.config_dir),
            "state": _snapshot_tree(install_home.state_dir),
        }
        before_path = install_home.environment()["PATH"]
        write_release(install_home.release, "2.0.0")
        result = subprocess.run(
            [str(install_home.launcher), "update", "2.0.0"],
            env=install_home.environment(FAKE_DOCKER_REJECT_V2_CONFIG="1"),
            capture_output=True,
            check=False,
            text=True,
        )

    assert result.returncode != 0
    assert {
        "bin": _snapshot_tree(install_home.bin_dir),
        "config": _snapshot_tree(install_home.config_dir),
        "state": _snapshot_tree(install_home.state_dir),
    } == before
    assert install_home.environment()["PATH"] == before_path


def test_version_and_doctor_skip_account_are_host_owned_and_start_no_agent_turn(
    install_home: InstallHome,
) -> None:
    result = install(install_home)
    assert result.returncode == 0, result.stderr
    install_home.docker_log.write_text("", encoding="utf-8")

    version = subprocess.run(
        [str(install_home.launcher), "version"],
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )
    doctor = subprocess.run(
        [str(install_home.launcher), "doctor", "--skip-account"],
        cwd=install_home.root,
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )

    assert version.returncode == 0, version.stderr
    assert "launcher_version=1.0.0" in version.stdout
    assert f"image_digest={DIGEST}" in version.stdout
    assert "source_revision=fixture-revision" in version.stdout
    assert doctor.returncode == 0, doctor.stderr
    assert "doctor: ok" in doctor.stdout
    docker_log = install_home.docker_log.read_text(encoding="utf-8")
    assert "config validate" in docker_log
    assert " models " not in f" {docker_log} "
    assert all(command not in docker_log for command in (" plan ", " run ", " task "))


def test_uninstall_preserves_config_and_durable_state_by_default(
    install_home: InstallHome,
) -> None:
    result = install(install_home)
    assert result.returncode == 0, result.stderr
    runs = install_home.state_dir / "runs" / "keep.json"
    runs.parent.mkdir()
    runs.write_text("{}\n", encoding="utf-8")

    removed = subprocess.run(
        [
            str(install_home.installed_uninstaller),
            "--state-dir",
            str(install_home.state_dir),
            "--yes",
        ],
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )

    assert removed.returncode == 0, removed.stderr
    assert not install_home.launcher.exists()
    assert not install_home.installed_installer.exists()
    assert not install_home.installed_uninstaller.exists()
    assert install_home.config.exists()
    assert runs.exists()
    assert not (install_home.state_dir / "install.json").exists()


def test_uninstall_purge_removes_only_exact_recorded_config_and_state(
    install_home: InstallHome,
) -> None:
    result = install(install_home)
    assert result.returncode == 0, result.stderr
    outside = install_home.home / "keep.txt"
    outside.write_text("keep\n", encoding="utf-8")

    removed = subprocess.run(
        [
            str(install_home.installed_uninstaller),
            "--state-dir",
            str(install_home.state_dir),
            "--purge",
            "--yes",
        ],
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )

    assert removed.returncode == 0, removed.stderr
    assert not install_home.config_dir.exists()
    assert not install_home.state_dir.exists()
    assert outside.read_text(encoding="utf-8") == "keep\n"


def test_consistently_forged_state_cannot_redirect_recursive_purge(
    install_home: InstallHome,
) -> None:
    result = install(install_home)
    assert result.returncode == 0, result.stderr
    valuable = install_home.home / ".config" / "valuable-project"
    valuable.mkdir()
    marker = valuable / "keep"
    marker.write_text("valuable", encoding="utf-8")
    (valuable / ".goalrouter-owned-v1").write_text(
        "goalrouter-owned-directory-v1", encoding="ascii"
    )
    (install_home.state_dir / "owned-config-dir").write_text(
        str(valuable), encoding="utf-8"
    )
    (install_home.state_dir / "guard-config-parent").write_text(
        str(valuable.parent), encoding="utf-8"
    )
    _rewrite_ownership_checksums(install_home.state_dir)

    rejected = subprocess.run(
        [
            str(install_home.installed_uninstaller),
            "--state-dir",
            str(install_home.state_dir),
            "--purge",
            "--yes",
        ],
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )

    assert rejected.returncode != 0
    assert marker.read_text(encoding="utf-8") == "valuable"


def test_purge_rejects_changed_current_xdg_config_root_before_any_delete(
    install_home: InstallHome,
) -> None:
    result = install(install_home)
    assert result.returncode == 0, result.stderr
    config_marker = install_home.config_dir / "keep"
    state_marker = install_home.state_dir / "runs" / "keep"
    config_marker.write_text("keep", encoding="utf-8")
    state_marker.parent.mkdir()
    state_marker.write_text("keep", encoding="utf-8")

    rejected = subprocess.run(
        [
            str(install_home.installed_uninstaller),
            "--state-dir",
            str(install_home.state_dir),
            "--purge",
            "--yes",
        ],
        env=install_home.environment(
            XDG_CONFIG_HOME=str(install_home.home / "different-config-root")
        ),
        capture_output=True,
        check=False,
        text=True,
    )

    assert rejected.returncode != 0
    assert config_marker.exists()
    assert state_marker.exists()
    assert install_home.launcher.exists()


@pytest.mark.parametrize("corruption", ["control", "home", "root", "xdg", "overlap"])
def test_purge_rejects_corrupt_broad_or_overlapping_ownership_before_any_delete(
    install_home: InstallHome, corruption: str
) -> None:
    result = install(install_home)
    assert result.returncode == 0, result.stderr
    config_marker = install_home.config_dir / "keep"
    state_marker = install_home.state_dir / "runs" / "keep"
    config_marker.write_text("keep", encoding="utf-8")
    state_marker.parent.mkdir()
    state_marker.write_text("keep", encoding="utf-8")
    owned_config = install_home.state_dir / "owned-config-dir"
    if corruption == "control":
        owned_config.write_bytes(b"bad\x01path\n")
    elif corruption == "home":
        owned_config.write_text(str(install_home.home) + "\n", encoding="utf-8")
    elif corruption == "root":
        owned_config.write_text("/\n", encoding="utf-8")
    elif corruption == "xdg":
        owned_config.write_text(str(install_home.home / ".config") + "\n", encoding="utf-8")
    else:
        owned_config.write_text(str(install_home.state_dir) + "\n", encoding="utf-8")
    _rewrite_ownership_checksums(install_home.state_dir)

    rejected = subprocess.run(
        [
            str(install_home.installed_uninstaller),
            "--state-dir",
            str(install_home.state_dir),
            "--purge",
            "--yes",
        ],
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )

    assert rejected.returncode != 0
    assert config_marker.exists()
    assert state_marker.exists()
    assert install_home.launcher.exists()


def _rewrite_ownership_checksums(state_dir: Path) -> None:
    names = [
        "install.json",
        "image-ref",
        "image-digest",
        "launcher-version",
        "protocol-version",
        "app-version",
        "image-platform",
        "source-revision",
        "release-base",
        "release-transport",
        "image-repository",
        "owned-home",
        "owned-bin-dir",
        "owned-config-dir",
        "owned-state-dir",
        "owned-launcher",
        "owned-installer",
        "owned-uninstaller",
        "guard-config-root",
        "guard-state-root",
        "guard-config-parent",
        "guard-state-parent",
        "guard-bin-parent",
    ]
    lines = [
        f"{hashlib.sha256((state_dir / name).read_bytes()).hexdigest()}  {name}"
        for name in names
    ]
    (state_dir / "ownership.sha256").write_text("\n".join(lines) + "\n", encoding="ascii")


@pytest.mark.parametrize(
    ("field", "command"),
    [
        ("owned-installer", ["update", "2.0.0"]),
        ("owned-uninstaller", ["uninstall", "--yes"]),
        ("release-base", ["version"]),
    ],
)
def test_maintenance_refuses_tampered_lifecycle_ownership_without_execution(
    install_home: InstallHome, field: str, command: list[str]
) -> None:
    result = install(install_home)
    assert result.returncode == 0, result.stderr
    sentinel = install_home.root / "sentinel"
    hostile = install_home.root / "hostile-installer"
    hostile.write_text(
        f"#!/bin/sh\nprintf attacked >'{sentinel}'\n", encoding="utf-8"
    )
    hostile.chmod(0o755)
    (install_home.state_dir / field).write_text(str(hostile), encoding="utf-8")

    rejected = subprocess.run(
        [str(install_home.launcher), *command],
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )

    assert rejected.returncode != 0
    assert not sentinel.exists()


def test_consistently_forged_state_cannot_redirect_lifecycle_execution(
    install_home: InstallHome,
) -> None:
    result = install(install_home)
    assert result.returncode == 0, result.stderr
    marker = install_home.root / "executed"
    payload = install_home.root / "payload"
    payload.write_text(f"#!/bin/sh\nprintf executed >'{marker}'\n", encoding="utf-8")
    payload.chmod(0o755)
    (install_home.state_dir / "owned-uninstaller").write_text(str(payload), encoding="utf-8")
    _rewrite_ownership_checksums(install_home.state_dir)

    rejected = subprocess.run(
        [str(install_home.launcher), "uninstall", "--yes"],
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )

    assert rejected.returncode != 0
    assert not marker.exists()


def test_maintenance_refuses_missing_install_manifest_without_container_fallback(
    install_home: InstallHome,
) -> None:
    result = install(install_home)
    assert result.returncode == 0, result.stderr
    (install_home.state_dir / "install.json").unlink()
    (install_home.home / ".codex").mkdir()
    install_home.docker_log.write_text("", encoding="utf-8")

    rejected = subprocess.run(
        [str(install_home.launcher), "version"],
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )

    assert rejected.returncode != 0
    assert "missing install metadata" in rejected.stderr
    assert install_home.docker_log.read_text(encoding="utf-8") == ""


def test_normal_launch_refuses_tampered_digest_before_container_execution(
    install_home: InstallHome,
) -> None:
    result = install(install_home)
    assert result.returncode == 0, result.stderr
    (install_home.home / ".codex").mkdir()
    (install_home.state_dir / "image-digest").write_text(
        "sha256:" + ("b" * 64), encoding="utf-8"
    )
    install_home.docker_log.write_text("", encoding="utf-8")

    rejected = subprocess.run(
        [str(install_home.launcher), "config", "validate"],
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )

    assert rejected.returncode != 0
    assert "ownership metadata checksum mismatch" in rejected.stderr
    assert install_home.docker_log.read_text(encoding="utf-8") == ""


def test_consistently_forged_state_cannot_select_runtime_image(
    install_home: InstallHome,
) -> None:
    result = install(install_home)
    assert result.returncode == 0, result.stderr
    (install_home.home / ".codex").mkdir()
    (install_home.state_dir / "image-ref").write_text("evil.example/payload", encoding="utf-8")
    (install_home.state_dir / "image-digest").write_text("sha256:" + ("b" * 64), encoding="ascii")
    _rewrite_ownership_checksums(install_home.state_dir)
    install_home.docker_log.write_text("", encoding="utf-8")

    rejected = subprocess.run(
        [str(install_home.launcher), "config", "validate"],
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )

    assert rejected.returncode != 0
    assert "evil.example" not in install_home.docker_log.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "command",
    [["config", "validate"], ["uninstall", "--yes"]],
    ids=["ordinary", "maintenance"],
)
def test_adjacent_control_prevents_legacy_fallback_when_all_state_markers_are_deleted(
    install_home: InstallHome, command: list[str]
) -> None:
    result = install(install_home)
    assert result.returncode == 0, result.stderr
    (install_home.home / ".codex").mkdir()
    for marker in (
        "install.json",
        "ownership.sha256",
        "owned-installer",
        "owned-uninstaller",
    ):
        (install_home.state_dir / marker).unlink()
    (install_home.state_dir / "image-ref").write_text(
        "evil.example/payload", encoding="ascii"
    )
    (install_home.state_dir / "image-digest").write_text(
        "sha256:" + ("b" * 64), encoding="ascii"
    )
    install_home.docker_log.write_text("", encoding="utf-8")

    rejected = subprocess.run(
        [str(install_home.launcher), *command],
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )

    assert rejected.returncode != 0
    assert "trusted" in rejected.stderr.lower()
    assert install_home.docker_log.read_text(encoding="utf-8") == ""
    assert install_home.installed_uninstaller.exists()


@pytest.mark.parametrize("invocation", ["path", "exec"], ids=["path", "shell-exec"])
def test_installed_launcher_discovers_adjacent_control_for_path_invocations(
    install_home: InstallHome, invocation: str
) -> None:
    result = install(install_home)
    assert result.returncode == 0, result.stderr
    (install_home.home / ".codex").mkdir()
    environment = install_home.environment()
    environment["PATH"] = f"{install_home.bin_dir}:{environment['PATH']}"
    command = (
        ["goalrouter", "config", "validate"]
        if invocation == "path"
        else ["/bin/sh", "-c", "exec goalrouter config validate"]
    )

    launched = subprocess.run(
        command,
        env=environment,
        cwd=install_home.root,
        capture_output=True,
        check=False,
        text=True,
    )

    assert launched.returncode == 0, launched.stderr
    assert f"registry.example/goalrouter@{DIGEST}" in install_home.docker_log.read_text(
        encoding="utf-8"
    )


def test_doctor_exclusive_probe_cannot_clobber_exec_preserved_pid_symlink(
    install_home: InstallHome,
) -> None:
    result = install(install_home)
    assert result.returncode == 0, result.stderr
    victim = install_home.root / "victim"
    victim.write_text("preserve-me", encoding="utf-8")

    doctor = subprocess.run(
        [
            "/bin/sh",
            "-c",
            'ln -s "$1" "$2/.goalrouter-doctor.$$"; exec "$3" doctor --skip-account',
            "sh",
            str(victim),
            str(install_home.state_dir),
            str(install_home.launcher),
        ],
        cwd=install_home.root,
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )

    assert doctor.returncode == 0, doctor.stderr
    assert victim.read_text(encoding="utf-8") == "preserve-me"


def test_shared_writable_install_parent_is_rejected_before_mutation(
    install_home: InstallHome,
) -> None:
    install_home.home.chmod(0o777)

    result = install(install_home)

    assert result.returncode != 0
    assert_no_installation(install_home)


@pytest.mark.parametrize("failure", ["doctor", "term"])
def test_reinstall_failure_preserves_preexisting_owned_directory_modes(
    install_home: InstallHome, failure: str
) -> None:
    first = install(install_home)
    assert first.returncode == 0, first.stderr
    (install_home.home / ".codex").mkdir()
    owned_dirs = [
        install_home.bin_dir,
        install_home.config_dir,
        install_home.state_dir,
        install_home.control_dir,
    ]
    for directory in owned_dirs:
        directory.chmod(0o755)
    environment: dict[str, str] = {}
    if failure == "doctor":
        environment["FAKE_DOCKER_MODELS_FAILURE"] = "1"
    else:
        mv_wrapper = install_home.fake_bin / "mv"
        mv_wrapper.write_text(
            "#!/bin/sh\n"
            "/bin/mv \"$@\"\n"
            "case \"$*\" in *.goalrouter-owned-v1.goalrouter-backup.*) "
            "kill -TERM \"$PPID\" ;; esac\n",
            encoding="utf-8",
        )
        mv_wrapper.chmod(0o755)

    with serve_release(install_home.release) as base:
        failed = run_installer(
            install_home,
            base,
            environment=environment,
            skip_doctor=False,
        )

    assert failed.returncode != 0
    assert [directory.stat().st_mode & 0o777 for directory in owned_dirs] == [
        0o755,
        0o755,
        0o755,
        0o755,
    ]


def test_purge_refuses_symlink_escape_and_preserves_all_targets(
    install_home: InstallHome,
) -> None:
    result = install(install_home)
    assert result.returncode == 0, result.stderr
    outside = install_home.root / "outside"
    outside.mkdir()
    marker = outside / "keep"
    marker.write_text("keep", encoding="utf-8")
    shutil.rmtree(install_home.config_dir)
    install_home.config_dir.symlink_to(outside, target_is_directory=True)

    rejected = subprocess.run(
        [
            str(install_home.installed_uninstaller),
            "--state-dir",
            str(install_home.state_dir),
            "--purge",
            "--yes",
        ],
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )

    assert rejected.returncode != 0
    assert marker.exists()
    assert install_home.launcher.exists()
    assert (install_home.state_dir / "install.json").exists()


def test_purge_refuses_recorded_custom_config_parent_before_any_delete(
    install_home: InstallHome,
) -> None:
    custom_bin = install_home.root / "custom-bin"
    config_parent = install_home.root / "custom-config-parent"
    custom_config = config_parent / "goalrouter"
    custom_state = install_home.root / "custom-state-parent" / "goalrouter"
    with serve_release(install_home.release) as base:
        installed = run_installer(
            install_home,
            base,
            "--bin-dir",
            str(custom_bin),
            "--config-dir",
            str(custom_config),
            "--state-dir",
            str(custom_state),
        )
    assert installed.returncode == 0, installed.stderr
    config_marker = custom_config / "keep"
    state_marker = custom_state / "keep"
    config_marker.write_text("keep", encoding="utf-8")
    state_marker.write_text("keep", encoding="utf-8")
    (custom_state / "owned-config-dir").write_text(
        str(config_parent), encoding="utf-8"
    )
    _rewrite_ownership_checksums(custom_state)

    rejected = subprocess.run(
        [
            str(custom_bin / "goalrouter-uninstall"),
            "--state-dir",
            str(custom_state),
            "--purge",
            "--yes",
        ],
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )

    assert rejected.returncode != 0
    assert config_marker.exists()
    assert state_marker.exists()
    assert (custom_bin / "goalrouter").exists()


def test_custom_install_uninstalls_from_trusted_recorded_config_path(
    install_home: InstallHome,
) -> None:
    custom_bin = install_home.root / "custom-bin"
    custom_config = install_home.root / "custom-config" / "goalrouter"
    custom_state = install_home.root / "custom-state" / "goalrouter"
    with serve_release(install_home.release) as base:
        installed = run_installer(
            install_home,
            base,
            "--bin-dir",
            str(custom_bin),
            "--config-dir",
            str(custom_config),
            "--state-dir",
            str(custom_state),
        )
    assert installed.returncode == 0, installed.stderr

    removed = subprocess.run(
        [
            str(custom_bin / "goalrouter"),
            "--state-dir",
            str(custom_state),
            "uninstall",
            "--yes",
        ],
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )

    assert removed.returncode == 0, removed.stderr
    assert not (custom_bin / "goalrouter").exists()
    assert custom_config.exists()
    assert custom_state.exists()


def test_custom_install_doctor_uses_selected_config_state_and_codex_paths(
    install_home: InstallHome,
) -> None:
    custom_bin = install_home.root / "doctor-bin"
    custom_config = install_home.root / "doctor-config" / "goalrouter"
    custom_state = install_home.root / "doctor-state" / "goalrouter"
    custom_codex = install_home.root / "doctor-codex"
    custom_codex.mkdir()

    with serve_release(install_home.release) as base:
        installed = run_installer(
            install_home,
            base,
            "--bin-dir",
            str(custom_bin),
            "--config-dir",
            str(custom_config),
            "--state-dir",
            str(custom_state),
            "--codex-home",
            str(custom_codex),
            skip_doctor=False,
        )

    assert installed.returncode == 0, installed.stderr
    assert (custom_state / "owned-codex-home").read_text(encoding="utf-8") == str(
        custom_codex
    )
    assert (custom_bin / ".goalrouter-control" / "owned-codex-home").exists()


def test_custom_update_propagates_trusted_codex_path_and_runs_doctor(
    install_home: InstallHome,
) -> None:
    custom_bin = install_home.root / "update-bin"
    custom_config = install_home.root / "update-config" / "goalrouter"
    custom_state = install_home.root / "update-state" / "goalrouter"
    custom_codex = install_home.root / "update-codex"
    custom_codex.mkdir()

    with serve_release(install_home.release) as base:
        installed = run_installer(
            install_home,
            base,
            "--bin-dir",
            str(custom_bin),
            "--config-dir",
            str(custom_config),
            "--state-dir",
            str(custom_state),
            "--codex-home",
            str(custom_codex),
            skip_doctor=False,
        )
        assert installed.returncode == 0, installed.stderr
        write_release(install_home.release, "2.0.0")
        updated = subprocess.run(
            [
                str(custom_bin / "goalrouter"),
                "--state-dir",
                str(custom_state),
                "update",
                "2.0.0",
            ],
            cwd=install_home.root,
            env=install_home.environment(FAKE_DOCKER_APP_VERSION="2.0.0"),
            capture_output=True,
            check=False,
            text=True,
        )

    assert updated.returncode == 0, updated.stderr
    assert (custom_state / "app-version").read_text(encoding="ascii") == "2.0.0"
    assert (custom_state / "owned-codex-home").read_text(encoding="utf-8") == str(
        custom_codex
    )


@pytest.mark.parametrize(
    "options",
    [
        ["--yes", "--yes"],
        ["--purge", "--purge", "--yes"],
        ["--yes", "--purge", "--yes"],
    ],
    ids=["yes", "purge", "mixed"],
)
def test_launcher_uninstall_rejects_duplicate_options_without_mutation(
    install_home: InstallHome, options: list[str]
) -> None:
    installed = install(install_home)
    assert installed.returncode == 0, installed.stderr

    rejected = subprocess.run(
        [str(install_home.launcher), "uninstall", *options],
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )

    assert rejected.returncode != 0
    assert "duplicate" in rejected.stderr.lower()
    assert install_home.launcher.exists()
    assert install_home.installed_uninstaller.exists()
    assert install_home.control_dir.exists()


@pytest.mark.parametrize(
    ("option", "value_name"),
    [
        ("--config", "config"),
        ("--state-dir", "state"),
        ("--codex-home", "codex"),
    ],
)
def test_installed_launcher_rejects_duplicate_lifecycle_path_options(
    install_home: InstallHome, option: str, value_name: str
) -> None:
    installed = install(install_home)
    assert installed.returncode == 0, installed.stderr
    (install_home.home / ".codex").mkdir()
    values = {
        "config": install_home.config_dir / "task-models.yaml",
        "state": install_home.state_dir,
        "codex": install_home.home / ".codex",
    }
    value = str(values[value_name])

    rejected = subprocess.run(
        [str(install_home.launcher), option, value, option, value, "uninstall", "--yes"],
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )

    assert rejected.returncode != 0
    assert "duplicate" in rejected.stderr.lower()
    assert install_home.launcher.exists()
    assert install_home.installed_uninstaller.exists()


@pytest.mark.parametrize(
    "options",
    [
        ["--yes", "--yes"],
        ["--purge", "--purge", "--yes"],
        ["--state-dir", "STATE", "--state-dir", "STATE", "--yes"],
        ["--config-dir", "CONFIG", "--config-dir", "CONFIG", "--yes"],
    ],
    ids=["yes", "purge", "state", "config"],
)
def test_uninstaller_rejects_duplicate_options_without_mutation(
    install_home: InstallHome, options: list[str]
) -> None:
    installed = install(install_home)
    assert installed.returncode == 0, installed.stderr
    expanded = [
        str(install_home.state_dir)
        if value == "STATE"
        else str(install_home.config_dir)
        if value == "CONFIG"
        else value
        for value in options
    ]

    rejected = subprocess.run(
        [str(install_home.installed_uninstaller), *expanded],
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )

    assert rejected.returncode != 0
    assert "duplicate" in rejected.stderr.lower()
    assert install_home.launcher.exists()
    assert install_home.installed_uninstaller.exists()
    assert install_home.control_dir.exists()


@pytest.mark.parametrize("entrypoint", ["launcher", "uninstaller"])
def test_preserving_uninstall_uses_trusted_paths_after_xdg_environment_changes(
    install_home: InstallHome, entrypoint: str
) -> None:
    installed = install(install_home)
    assert installed.returncode == 0, installed.stderr
    environment = install_home.environment(
        XDG_CONFIG_HOME=str(install_home.root / "different-config"),
        XDG_STATE_HOME=str(install_home.root / "different-state"),
    )
    command = (
        [str(install_home.launcher), "uninstall", "--yes"]
        if entrypoint == "launcher"
        else [str(install_home.installed_uninstaller), "--yes"]
    )

    removed = subprocess.run(
        command,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )

    assert removed.returncode == 0, removed.stderr
    assert not install_home.launcher.exists()
    assert install_home.config_dir.exists()
    assert install_home.state_dir.exists()


def test_uninstaller_accepts_relative_dot_path_from_physical_bin(
    install_home: InstallHome,
) -> None:
    installed = install(install_home)
    assert installed.returncode == 0, installed.stderr

    removed = subprocess.run(
        ["./goalrouter-uninstall", "--yes"],
        cwd=install_home.bin_dir,
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )

    assert removed.returncode == 0, removed.stderr
    assert not install_home.installed_uninstaller.exists()
    assert not install_home.control_dir.exists()
    assert install_home.config_dir.exists()
    assert install_home.state_dir.exists()


@pytest.mark.parametrize("phase", ["state", "launcher", "installer"])
def test_interrupted_preserving_uninstall_is_retryable(
    install_home: InstallHome, phase: str
) -> None:
    installed = install(install_home)
    assert installed.returncode == 0, installed.stderr
    rm_wrapper = install_home.fake_bin / "rm"
    rm_wrapper.write_text(
        "#!/bin/sh\n"
        "/bin/rm \"$@\"\n"
        "status=$?\n"
        "case \"$*\" in *\"${FAKE_RM_SIGNAL_MATCH:-__never__}\"*) "
        "[ -z \"${FAKE_RM_SIGNAL_MATCH:-}\" ] || kill -TERM \"$PPID\" ;; esac\n"
        "exit \"$status\"\n",
        encoding="utf-8",
    )
    rm_wrapper.chmod(0o755)
    match = {
        "state": str(install_home.state_dir / "install.json"),
        "launcher": str(install_home.launcher),
        "installer": str(install_home.installed_installer),
    }[phase]

    interrupted = subprocess.run(
        [str(install_home.installed_uninstaller), "--yes"],
        env=install_home.environment(FAKE_RM_SIGNAL_MATCH=match),
        capture_output=True,
        check=False,
        text=True,
    )

    assert interrupted.returncode != 0
    assert "retry" in interrupted.stderr.lower()
    assert install_home.installed_uninstaller.exists()
    assert install_home.control_dir.exists()

    retried = subprocess.run(
        [str(install_home.installed_uninstaller), "--yes"],
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )
    assert retried.returncode == 0, retried.stderr
    assert not install_home.launcher.exists()
    assert not install_home.installed_uninstaller.exists()
    assert not install_home.control_dir.exists()
    assert install_home.config_dir.exists()
    assert install_home.state_dir.exists()


def test_launcher_started_uninstall_reports_physical_retry_path(
    install_home: InstallHome,
) -> None:
    installed = install(install_home)
    assert installed.returncode == 0, installed.stderr
    rm_wrapper = install_home.fake_bin / "rm"
    rm_wrapper.write_text(
        "#!/bin/sh\n"
        "/bin/rm \"$@\"\n"
        "status=$?\n"
        "case \"$*\" in *\"${FAKE_RM_SIGNAL_MATCH:-__never__}\"*) "
        "[ -z \"${FAKE_RM_SIGNAL_MATCH:-}\" ] || kill -TERM \"$PPID\" ;; esac\n"
        "exit \"$status\"\n",
        encoding="utf-8",
    )
    rm_wrapper.chmod(0o755)

    interrupted = subprocess.run(
        [str(install_home.launcher), "uninstall", "--yes"],
        env=install_home.environment(
            FAKE_RM_SIGNAL_MATCH=str(install_home.state_dir / "install.json")
        ),
        capture_output=True,
        check=False,
        text=True,
    )

    assert interrupted.returncode != 0
    assert str(install_home.installed_uninstaller) in interrupted.stderr
    assert "--yes" in interrupted.stderr
    retried = subprocess.run(
        [str(install_home.installed_uninstaller), "--yes"],
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )
    assert retried.returncode == 0, retried.stderr
    assert not install_home.control_dir.exists()


@pytest.mark.parametrize("phase", ["config", "state"])
def test_interrupted_purge_uninstall_is_retryable_and_stays_exact(
    install_home: InstallHome, phase: str
) -> None:
    installed = install(install_home)
    assert installed.returncode == 0, installed.stderr
    outside = install_home.home / "outside-marker"
    outside.write_text("preserve", encoding="utf-8")
    find_wrapper = install_home.fake_bin / "find"
    find_wrapper.write_text(
        "#!/bin/sh\n"
        "/usr/bin/find \"$@\"\n"
        "status=$?\n"
        "if [ \"$status\" -eq 0 ] && [ \"${1:-}\" = \"${FAKE_FIND_SIGNAL_TARGET:-}\" ]; then "
        "kill -TERM \"$PPID\"; fi\n"
        "exit \"$status\"\n",
        encoding="utf-8",
    )
    find_wrapper.chmod(0o755)
    target = install_home.config_dir if phase == "config" else install_home.state_dir

    interrupted = subprocess.run(
        [str(install_home.installed_uninstaller), "--purge", "--yes"],
        env=install_home.environment(FAKE_FIND_SIGNAL_TARGET=str(target)),
        capture_output=True,
        check=False,
        text=True,
    )

    assert interrupted.returncode != 0
    assert "retry" in interrupted.stderr.lower()
    assert install_home.installed_uninstaller.exists()
    assert install_home.control_dir.exists()
    assert outside.read_text(encoding="utf-8") == "preserve"

    retried = subprocess.run(
        [str(install_home.installed_uninstaller), "--purge", "--yes"],
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )
    assert retried.returncode == 0, retried.stderr
    assert not install_home.config_dir.exists()
    assert not install_home.state_dir.exists()
    assert not install_home.control_dir.exists()
    assert outside.read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize("interruption", ["term", "int", "failure"])
def test_mid_tree_purge_interruption_preserves_sentinel_and_is_retryable(
    install_home: InstallHome, interruption: str
) -> None:
    installed = install(install_home)
    assert installed.returncode == 0, installed.stderr
    nested = install_home.config_dir / "midtree"
    nested.mkdir()
    deleted = nested / "deleted"
    remaining = nested / "remaining"
    deleted.write_text("delete-first", encoding="utf-8")
    remaining.write_text("preserve-until-retry", encoding="utf-8")
    outside = install_home.home / "midtree-outside"
    outside.write_text("outside", encoding="utf-8")
    find_wrapper = install_home.fake_bin / "find"
    find_wrapper.write_text(
        "#!/bin/sh\n"
        "if [ \"${FAKE_FIND_MIDTREE_TARGET:-}\" = \"${1:-}\" ]; then\n"
        "  /bin/rm -f \"$1/midtree/deleted\"\n"
        "  case \" $* \" in *'.goalrouter-owned-v1'*) ;; "
        "*) /bin/rm -f \"$1/.goalrouter-owned-v1\" ;; esac\n"
        "  case \"${FAKE_FIND_MIDTREE_ACTION:-}\" in\n"
        "    term) kill -TERM \"$PPID\"; exit 143 ;;\n"
        "    int) kill -INT \"$PPID\"; exit 130 ;;\n"
        "    failure) exit 47 ;;\n"
        "  esac\n"
        "fi\n"
        "exec /usr/bin/find \"$@\"\n",
        encoding="utf-8",
    )
    find_wrapper.chmod(0o755)

    interrupted = subprocess.run(
        [str(install_home.installed_uninstaller), "--purge", "--yes"],
        env=install_home.environment(
            FAKE_FIND_MIDTREE_TARGET=str(install_home.config_dir),
            FAKE_FIND_MIDTREE_ACTION=interruption,
        ),
        capture_output=True,
        check=False,
        text=True,
    )

    assert interrupted.returncode != 0
    assert not deleted.exists()
    assert remaining.exists()
    assert (install_home.config_dir / ".goalrouter-owned-v1").is_file()
    assert install_home.installed_uninstaller.exists()
    assert install_home.control_dir.exists()
    assert (install_home.control_dir / ".uninstalling").read_text() == "purge"
    assert outside.read_text(encoding="utf-8") == "outside"

    retried = subprocess.run(
        [str(install_home.installed_uninstaller), "--purge", "--yes"],
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )
    assert retried.returncode == 0, retried.stderr
    assert not install_home.config_dir.exists()
    assert not install_home.state_dir.exists()
    assert not install_home.control_dir.exists()
    assert outside.read_text(encoding="utf-8") == "outside"


def test_active_purge_retry_accepts_only_empty_exact_root_without_sentinel(
    install_home: InstallHome,
) -> None:
    installed = install(install_home)
    assert installed.returncode == 0, installed.stderr
    outside = install_home.home / "final-gap-outside"
    outside.write_text("outside", encoding="utf-8")
    rmdir_wrapper = install_home.fake_bin / "rmdir"
    rmdir_wrapper.write_text(
        "#!/bin/sh\n"
        "if [ \"${FAKE_RMDIR_KILL_TARGET:-}\" = \"${1:-}\" ]; then\n"
        "  [ ! -e \"$1/.goalrouter-owned-v1\" ] || exit 92\n"
        "  kill -KILL \"$PPID\"\n"
        "  exit 137\n"
        "fi\n"
        "exec /bin/rmdir \"$@\"\n",
        encoding="utf-8",
    )
    rmdir_wrapper.chmod(0o755)

    interrupted = subprocess.run(
        [str(install_home.installed_uninstaller), "--purge", "--yes"],
        env=install_home.environment(
            FAKE_RMDIR_KILL_TARGET=str(install_home.config_dir)
        ),
        capture_output=True,
        check=False,
        text=True,
    )

    assert interrupted.returncode != 0
    assert install_home.config_dir.is_dir()
    assert list(install_home.config_dir.iterdir()) == []
    assert install_home.installed_uninstaller.exists()
    assert install_home.control_dir.exists()
    assert outside.read_text(encoding="utf-8") == "outside"

    retried = subprocess.run(
        [str(install_home.installed_uninstaller), "--purge", "--yes"],
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )
    assert retried.returncode == 0, retried.stderr
    assert not install_home.config_dir.exists()
    assert not install_home.state_dir.exists()
    assert not install_home.control_dir.exists()
    assert outside.read_text(encoding="utf-8") == "outside"


def test_active_purge_retry_refuses_nonempty_root_without_sentinel(
    install_home: InstallHome,
) -> None:
    installed = install(install_home)
    assert installed.returncode == 0, installed.stderr
    durable = install_home.config_dir / "durable"
    durable.write_text("must-not-delete", encoding="utf-8")
    find_wrapper = install_home.fake_bin / "find"
    find_wrapper.write_text(
        "#!/bin/sh\n"
        "if [ \"${FAKE_FIND_FAIL_TARGET:-}\" = \"${1:-}\" ]; then exit 47; fi\n"
        "exec /usr/bin/find \"$@\"\n",
        encoding="utf-8",
    )
    find_wrapper.chmod(0o755)
    interrupted = subprocess.run(
        [str(install_home.installed_uninstaller), "--purge", "--yes"],
        env=install_home.environment(
            FAKE_FIND_FAIL_TARGET=str(install_home.config_dir)
        ),
        capture_output=True,
        check=False,
        text=True,
    )
    assert interrupted.returncode != 0
    (install_home.config_dir / ".goalrouter-owned-v1").unlink()

    rejected = subprocess.run(
        [str(install_home.installed_uninstaller), "--purge", "--yes"],
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )

    assert rejected.returncode != 0
    assert "nonempty" in rejected.stderr.lower()
    assert durable.read_text(encoding="utf-8") == "must-not-delete"
    assert install_home.state_dir.exists()
    assert install_home.installed_uninstaller.exists()
    assert install_home.control_dir.exists()


def test_active_purge_retry_refuses_dangling_exact_root_symlink(
    install_home: InstallHome,
) -> None:
    installed = install(install_home)
    assert installed.returncode == 0, installed.stderr
    find_wrapper = install_home.fake_bin / "find"
    find_wrapper.write_text(
        "#!/bin/sh\n"
        "if [ \"${FAKE_FIND_FAIL_TARGET:-}\" = \"${1:-}\" ]; then exit 47; fi\n"
        "exec /usr/bin/find \"$@\"\n",
        encoding="utf-8",
    )
    find_wrapper.chmod(0o755)
    stopped = subprocess.run(
        [str(install_home.installed_uninstaller), "--purge", "--yes"],
        env=install_home.environment(
            FAKE_FIND_FAIL_TARGET=str(install_home.config_dir)
        ),
        capture_output=True,
        check=False,
        text=True,
    )
    assert stopped.returncode != 0
    shutil.rmtree(install_home.config_dir)
    install_home.config_dir.symlink_to(
        install_home.root / "missing-config-target", target_is_directory=True
    )

    rejected = subprocess.run(
        [str(install_home.installed_uninstaller), "--purge", "--yes"],
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )

    assert rejected.returncode != 0
    assert "symbolic link" in rejected.stderr.lower()
    assert install_home.config_dir.is_symlink()
    assert install_home.state_dir.exists()
    assert install_home.installed_uninstaller.exists()
    assert install_home.control_dir.exists()

def test_install_refuses_active_interrupted_purge_recovery_marker(
    install_home: InstallHome,
) -> None:
    installed = install(install_home)
    assert installed.returncode == 0, installed.stderr
    find_wrapper = install_home.fake_bin / "find"
    find_wrapper.write_text(
        "#!/bin/sh\n"
        "/usr/bin/find \"$@\"\n"
        "status=$?\n"
        "if [ \"$status\" -eq 0 ] && [ \"${1:-}\" = \"${FAKE_FIND_SIGNAL_TARGET:-}\" ]; then "
        "kill -TERM \"$PPID\"; fi\n"
        "exit \"$status\"\n",
        encoding="utf-8",
    )
    find_wrapper.chmod(0o755)
    interrupted = subprocess.run(
        [str(install_home.installed_uninstaller), "--purge", "--yes"],
        env=install_home.environment(
            FAKE_FIND_SIGNAL_TARGET=str(install_home.config_dir)
        ),
        capture_output=True,
        check=False,
        text=True,
    )
    assert interrupted.returncode != 0
    assert (install_home.control_dir / ".uninstalling").exists()
    install_home.docker_log.write_text("", encoding="utf-8")

    rejected = install(install_home)

    assert rejected.returncode != 0
    assert "resume uninstall" in rejected.stderr.lower()
    assert (install_home.control_dir / ".uninstalling").exists()
    assert install_home.config_dir.is_dir()
    assert [path.name for path in install_home.config_dir.iterdir()] == [
        ".goalrouter-owned-v1"
    ]
    assert install_home.state_dir.exists()
    assert install_home.docker_log.read_text(encoding="utf-8") == ""


def test_uninstall_retry_cleans_interrupted_marker_staging_and_completes(
    install_home: InstallHome,
) -> None:
    installed = install(install_home)
    assert installed.returncode == 0, installed.stderr
    mktemp_wrapper = install_home.fake_bin / "mktemp"
    mktemp_wrapper.write_text(
        "#!/bin/sh\n"
        "staged=$(/bin/mktemp \"$@\") || exit $?\n"
        "printf '%s\\n' \"$staged\"\n"
        "[ \"${FAKE_MKTEMP_SIGNAL:-0}\" -eq 0 ] || kill -TERM \"$PPID\"\n",
        encoding="utf-8",
    )
    mktemp_wrapper.chmod(0o755)

    interrupted = subprocess.run(
        [str(install_home.installed_uninstaller), "--yes"],
        env=install_home.environment(FAKE_MKTEMP_SIGNAL="1"),
        capture_output=True,
        check=False,
        text=True,
    )

    assert interrupted.returncode != 0
    assert install_home.installed_uninstaller.exists()
    assert install_home.control_dir.exists()

    retried = subprocess.run(
        [str(install_home.installed_uninstaller), "--yes"],
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )
    assert retried.returncode == 0, retried.stderr
    assert not install_home.installed_uninstaller.exists()
    assert not install_home.control_dir.exists()
    assert install_home.config_dir.exists()
    assert install_home.state_dir.exists()

def test_interruption_cleans_staging_without_partial_install(install_home: InstallHome) -> None:
    fake_curl = install_home.fake_bin / "curl"
    fake_curl.write_text(
        "#!/bin/sh\nkill -TERM \"$PPID\"\nexit 143\n",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)

    result = subprocess.run(
        [
            str(INSTALLER),
            "--version",
            "1.0.0",
            "--release-base",
            "https://example.invalid/releases",
            "--image",
            "registry.example/goalrouter:1.0.0",
            "--yes",
            "--skip-doctor",
        ],
        env=install_home.environment(),
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode in {128 + signal.SIGTERM, 143}
    assert_no_installation(install_home)
    assert list(install_home.home.rglob("*.goalrouter-tmp.*")) == []


def test_signal_after_backup_move_restores_active_file_without_stranding_backup(
    install_home: InstallHome,
) -> None:
    result = install(install_home)
    assert result.returncode == 0, result.stderr
    config_sentinel = install_home.config_dir / ".goalrouter-owned-v1"
    state_sentinel = install_home.state_dir / ".goalrouter-owned-v1"
    mv_wrapper = install_home.fake_bin / "mv"
    mv_wrapper.write_text(
        "#!/bin/sh\n"
        "/bin/mv \"$@\"\n"
        "case \"$*\" in *.goalrouter-owned-v1.goalrouter-backup.*) kill -TERM \"$PPID\" ;; esac\n",
        encoding="utf-8",
    )
    mv_wrapper.chmod(0o755)

    interrupted = install(install_home)

    assert interrupted.returncode in {-signal.SIGTERM, 128 + signal.SIGTERM, 143}
    assert config_sentinel.read_text(encoding="ascii") == "goalrouter-owned-directory-v1"
    assert state_sentinel.read_text(encoding="ascii") == "goalrouter-owned-directory-v1"
    assert list(install_home.home.rglob("*.goalrouter-backup.*")) == []
    assert list(install_home.home.rglob("*.goalrouter-tmp.*")) == []
