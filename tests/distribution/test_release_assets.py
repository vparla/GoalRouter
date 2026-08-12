# SPDX-License-Identifier: MIT
# File: tests/distribution/test_release_assets.py
# Purpose: Prove deterministic, atomic, checksum-rooted release assets

import hashlib
import json
import shutil
import stat
import subprocess
import tarfile
import time
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
BUILDER = ROOT / "scripts" / "release-assets.sh"
VERSION = "1.0.8"
TAG = "v1.0.8"
IMAGE = "ghcr.io/vparla/goalrouter:1.0.8"
DIGEST = f"sha256:{'a' * 64}"
REVISION = "b" * 40
EPOCH = 1_700_000_000
MAX_GZIP_EPOCH = 4_294_967_295
ARCHITECTURES = ["linux/amd64", "linux/arm64"]
MINIMUM_HOSTS = {
    "windows": "10.0.19045",
    "powershell": "5.1",
    "wsl": "2.2.3",
    "docker": "20.10",
}
CHECKSUM_ORDER = [
    "goalrouter-1.0.8-unix.tar.gz",
    "goalrouter-1.0.8-windows.zip",
    "install.ps1",
    "install.sh",
    "release-manifest.json",
    "uninstall.ps1",
    "uninstall.sh",
]
RELEASE_FILES = {"SHA256SUMS", *CHECKSUM_ORDER}


def _arguments(output: Path) -> list[str]:
    return [
        "--version",
        VERSION,
        "--tag",
        TAG,
        "--image",
        IMAGE,
        "--image-digest",
        DIGEST,
        "--source-revision",
        REVISION,
        "--source-date-epoch",
        str(EPOCH),
        "--output-dir",
        str(output),
    ]


def _run_builder(
    output: Path,
    *,
    arguments: Sequence[str] | None = None,
    builder: Path = BUILDER,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(builder), *(arguments or _arguments(output))],
        cwd=builder.parent.parent,
        check=False,
        capture_output=True,
        text=True,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_hashes(output: Path) -> dict[str, str]:
    return {path.name: _sha256(path) for path in sorted(output.iterdir())}


def _manifest_bytes() -> bytes:
    payload = {
        "version": VERSION,
        "protocol_version": 1,
        "image": IMAGE,
        "image_digest": DIGEST,
        "architectures": ARCHITECTURES,
        "source_revision": REVISION,
        "minimum_hosts": MINIMUM_HOSTS,
    }
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"


def _copy_builder_source(destination_root: Path) -> Path:
    for relative in (
        "scripts/release-assets.sh",
        "scripts/goalrouter",
        "scripts/goalrouter.cmd",
        "scripts/goalrouter.ps1",
        "scripts/install.ps1",
        "scripts/install.sh",
        "scripts/uninstall.ps1",
        "scripts/uninstall.sh",
        "src/goalrouter/__init__.py",
        "src/goalrouter/build_info.py",
        "pyproject.toml",
        "Dockerfile",
    ):
        source = ROOT / relative
        destination = destination_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    return destination_root / "scripts/release-assets.sh"


def _assert_release_tree(output: Path) -> None:
    assert {path.name for path in output.iterdir()} == RELEASE_FILES
    assert all(path.is_file() and not path.is_symlink() for path in output.iterdir())
    assert (output / "release-manifest.json").read_bytes() == _manifest_bytes()

    checksum_lines = (output / "SHA256SUMS").read_text(encoding="ascii").splitlines()
    assert [line.split("  ", 1)[1] for line in checksum_lines] == CHECKSUM_ORDER
    for line, name in zip(checksum_lines, CHECKSUM_ORDER, strict=True):
        assert line == f"{_sha256(output / name)}  {name}"


@pytest.fixture
def release_tree(tmp_path: Path) -> Path:
    output = tmp_path / "release"
    output.mkdir(mode=0o700)
    result = _run_builder(output)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    return output


def test_two_release_runs_are_byte_identical_and_checksum_rooted(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir(mode=0o700)
    second.mkdir(mode=0o700)

    first_result = _run_builder(first)
    second_result = _run_builder(second)

    assert first_result.returncode == second_result.returncode == 0
    assert first_result.stdout == first_result.stderr == ""
    assert second_result.stdout == second_result.stderr == ""
    _assert_release_tree(first)
    _assert_release_tree(second)
    assert _tree_hashes(first) == _tree_hashes(second)


def test_release_manifest_is_canonical_and_raw_assets_are_exact_source_bytes(
    release_tree: Path,
) -> None:
    assert (release_tree / "release-manifest.json").read_bytes() == _manifest_bytes()
    for name in ("install.ps1", "install.sh", "uninstall.ps1", "uninstall.sh"):
        assert (release_tree / name).read_bytes() == (ROOT / "scripts" / name).read_bytes()


def test_unix_archive_has_only_safe_sorted_deterministic_regular_members(
    release_tree: Path,
) -> None:
    archive = release_tree / "goalrouter-1.0.8-unix.tar.gz"
    with archive.open("rb") as stream:
        header = stream.read(10)
    assert header[:3] == b"\x1f\x8b\x08"
    assert int.from_bytes(header[4:8], "little") == EPOCH
    assert header[9] == 255

    with tarfile.open(archive, mode="r:gz") as tar:
        members = tar.getmembers()
        assert [member.name for member in members] == [
            "goalrouter",
            "install.sh",
            "uninstall.sh",
        ]
        for member in members:
            assert member.isfile()
            assert member.name == Path(member.name).name
            assert member.mode == 0o755
            assert member.mtime == EPOCH
            assert member.uid == member.gid == 0
            assert member.uname == member.gname == ""
        expected_sources = {
            "goalrouter": ROOT / "scripts" / "goalrouter",
            "install.sh": ROOT / "scripts" / "install.sh",
            "uninstall.sh": ROOT / "scripts" / "uninstall.sh",
        }
        for member in members:
            extracted = tar.extractfile(member)
            assert extracted is not None
            assert extracted.read() == expected_sources[member.name].read_bytes()


def test_windows_archive_has_only_safe_sorted_deterministic_regular_members(
    release_tree: Path,
) -> None:
    expected_timestamp = time.gmtime(EPOCH)[:6]
    archive = release_tree / "goalrouter-1.0.8-windows.zip"
    with zipfile.ZipFile(archive, mode="r") as zipped:
        members = zipped.infolist()
        assert [member.filename for member in members] == [
            "goalrouter.cmd",
            "goalrouter.ps1",
            "install.ps1",
            "uninstall.ps1",
        ]
        for member in members:
            mode = member.external_attr >> 16
            assert stat.S_ISREG(mode)
            assert stat.S_IMODE(mode) == 0o755
            assert member.filename == Path(member.filename).name
            assert member.date_time == expected_timestamp
            assert member.create_system == 3
            assert member.extra == b""
            assert member.comment == b""
            assert zipped.read(member) == (ROOT / "scripts" / member.filename).read_bytes()


@pytest.mark.parametrize(
    ("option", "invalid"),
    [
        ("--version", "1.0"),
        ("--version", "1.0.9"),
        ("--tag", "1.0.0"),
        ("--tag", "v1.0.9"),
        ("--image", "ghcr.io/vparla/goalrouter:latest"),
        ("--image", "GHCR.IO/vparla/goalrouter:1.0.0"),
        ("--image-digest", f"sha256:{'A' * 64}"),
        ("--image-digest", f"sha512:{'a' * 64}"),
        ("--source-revision", "local"),
        ("--source-revision", "A" * 40),
        ("--source-date-epoch", "not-an-epoch"),
        ("--source-date-epoch", "315532799"),
        ("--source-date-epoch", "4294967296"),
        ("--source-date-epoch", "4354819200"),
    ],
)
def test_invalid_inputs_fail_explicitly_without_partial_publication(
    tmp_path: Path, option: str, invalid: str
) -> None:
    output = tmp_path / "release"
    output.mkdir(mode=0o700)
    arguments = _arguments(output)
    arguments[arguments.index(option) + 1] = invalid

    result = _run_builder(output, arguments=arguments)

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr.startswith("goalrouter release assets: ")
    assert "Traceback" not in result.stderr
    assert list(output.iterdir()) == []


def test_maximum_gzip_epoch_is_accepted(tmp_path: Path) -> None:
    output = tmp_path / "release"
    output.mkdir(mode=0o700)
    arguments = _arguments(output)
    arguments[arguments.index("--source-date-epoch") + 1] = str(MAX_GZIP_EPOCH)

    result = _run_builder(output, arguments=arguments)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert {path.name for path in output.iterdir()} == RELEASE_FILES
    archive = output / f"goalrouter-{VERSION}-unix.tar.gz"
    assert int.from_bytes(archive.read_bytes()[4:8], "little") == MAX_GZIP_EPOCH


def test_output_must_be_unambiguous_empty_owned_directory(tmp_path: Path) -> None:
    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    sentinel = nonempty / "sentinel"
    sentinel.write_text("preserve", encoding="utf-8")
    nonempty_result = _run_builder(nonempty)
    assert nonempty_result.returncode != 0
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert {path.name for path in nonempty.iterdir()} == {"sentinel"}

    target = tmp_path / "target"
    target.mkdir()
    symlink = tmp_path / "symlink"
    symlink.symlink_to(target, target_is_directory=True)
    symlink_result = _run_builder(symlink)
    assert symlink_result.returncode != 0
    assert list(target.iterdir()) == []

    relative = Path("relative-release-output")
    relative_result = _run_builder(relative)
    assert relative_result.returncode != 0
    assert not (ROOT / relative).exists()


def test_version_surface_drift_fails_before_publication(tmp_path: Path) -> None:
    staged_root = tmp_path / "source"
    builder = _copy_builder_source(staged_root)
    init_path = staged_root / "src/goalrouter/__init__.py"
    init_path.write_text(
        init_path.read_text(encoding="utf-8").replace('"1.0.8"', '"1.0.9"'),
        encoding="utf-8",
    )
    output = tmp_path / "release"
    output.mkdir()

    result = _run_builder(
        output,
        builder=builder,
    )

    assert result.returncode != 0
    assert "version" in result.stderr.lower()
    assert list(output.iterdir()) == []


def test_protocol_surface_drift_fails_before_publication(tmp_path: Path) -> None:
    staged_root = tmp_path / "source"
    builder = _copy_builder_source(staged_root)
    build_info = staged_root / "src/goalrouter/build_info.py"
    build_info.write_text(
        build_info.read_text(encoding="utf-8").replace("protocol_version=1", "protocol_version=2"),
        encoding="utf-8",
    )
    output = tmp_path / "release"
    output.mkdir()

    result = _run_builder(output, builder=builder)

    assert result.returncode != 0
    assert "protocol" in result.stderr.lower()
    assert list(output.iterdir()) == []


def test_final_directory_mode_failure_preserves_empty_destination(tmp_path: Path) -> None:
    staged_root = tmp_path / "source"
    builder = _copy_builder_source(staged_root)
    source = builder.read_text(encoding="utf-8")
    mode_commit = "        staging.chmod(output_mode)\n"
    destination_remove = "        output.rmdir()\n"
    atomic_replace = "        os.replace(staging, output)\n"
    assert source.count(mode_commit) == 1
    assert source.index(mode_commit) < source.index(destination_remove) < source.index(
        atomic_replace
    )
    success_tail = source.split(atomic_replace, maxsplit=1)[1].split(
        "    except BaseException:", maxsplit=1
    )[0]
    assert "output.chmod(output_mode)" not in success_tail
    builder.write_text(
        source.replace(
            mode_commit,
            '        raise OSError("simulated final directory mode failure")\n',
        ),
        encoding="utf-8",
    )
    output = tmp_path / "release"
    output.mkdir(mode=0o750)
    original_mode = stat.S_IMODE(output.stat().st_mode)

    result = _run_builder(output, builder=builder)

    assert result.returncode != 0
    assert "simulated final directory mode failure" in result.stderr
    assert output.is_dir() and not output.is_symlink()
    assert list(output.iterdir()) == []
    assert stat.S_IMODE(output.stat().st_mode) == original_mode
    assert not list(tmp_path.glob(".goalrouter-release-*"))


def test_atomic_replace_failure_restores_exact_empty_destination(tmp_path: Path) -> None:
    staged_root = tmp_path / "source"
    builder = _copy_builder_source(staged_root)
    source = builder.read_text(encoding="utf-8")
    atomic_replace = "        os.replace(staging, output)\n"
    assert source.count(atomic_replace) == 1
    builder.write_text(
        source.replace(
            atomic_replace,
            '        raise OSError("simulated atomic replace failure")\n',
        ),
        encoding="utf-8",
    )
    output = tmp_path / "release"
    output.mkdir(mode=0o750)
    original_mode = stat.S_IMODE(output.stat().st_mode)
    assert original_mode == 0o750

    result = _run_builder(output, builder=builder)

    assert result.returncode != 0
    assert "simulated atomic replace failure" in result.stderr
    assert output.is_dir() and not output.is_symlink()
    assert list(output.iterdir()) == []
    assert stat.S_IMODE(output.stat().st_mode) == original_mode
    assert not list(tmp_path.glob(".goalrouter-release-*"))


def test_release_tree_validator_rejects_manifest_checksum_and_member_drift(
    release_tree: Path,
) -> None:
    manifest = release_tree / "release-manifest.json"
    original_manifest = manifest.read_bytes()
    manifest.write_bytes(
        original_manifest.replace(b'"protocol_version":1', b'"protocol_version":2')
    )
    with pytest.raises(AssertionError):
        _assert_release_tree(release_tree)
    manifest.write_bytes(original_manifest)

    checksums = release_tree / "SHA256SUMS"
    original_checksums = checksums.read_bytes()
    checksums.write_bytes(original_checksums.replace(b"  install.sh", b"  ../install.sh"))
    with pytest.raises(AssertionError):
        _assert_release_tree(release_tree)
    checksums.write_bytes(original_checksums)

    extra = release_tree / "extra"
    extra.write_bytes(b"unexpected")
    with pytest.raises(AssertionError):
        _assert_release_tree(release_tree)


def test_release_manifest_shape_and_order_are_shared_with_installers(
    release_tree: Path,
) -> None:
    manifest = json.loads((release_tree / "release-manifest.json").read_bytes())
    assert list(manifest) == [
        "version",
        "protocol_version",
        "image",
        "image_digest",
        "architectures",
        "source_revision",
        "minimum_hosts",
    ]
    assert manifest["architectures"] == ARCHITECTURES
    assert isinstance(manifest["minimum_hosts"], Mapping)
    assert list(manifest["minimum_hosts"]) == list(MINIMUM_HOSTS)
    for installer in (ROOT / "scripts/install.sh", ROOT / "scripts/install.ps1"):
        source = installer.read_text(encoding="utf-8")
        for key in MINIMUM_HOSTS:
            assert key in source


def test_declared_release_builder_is_bounded_and_shellchecked() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM base AS release-assets" in dockerfile
    assert "ENTRYPOINT [\"/workspace/scripts/release-assets.sh\"]" in dockerfile
    assert "org.opencontainers.image.title=\"GoalRouter\"" in dockerfile
    assert "org.opencontainers.image.description=" in dockerfile
    assert "org.opencontainers.image.documentation=" in dockerfile

    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    service = compose["services"]["release-assets"]
    assert service == {
        "image": "goalrouter-release-assets:local",
        "build": {"context": ".", "target": "release-assets"},
        "entrypoint": ["/workspace/scripts/release-assets.sh"],
        "network_mode": "none",
        "read_only": True,
        "tmpfs": ["/tmp:rw,exec,nosuid,size=256m,mode=1777"],
    }
    shellcheck = compose["services"]["shellcheck"]["command"]
    assert shellcheck.count("scripts/release-assets.sh") == 1
