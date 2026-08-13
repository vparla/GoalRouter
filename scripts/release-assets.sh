#!/bin/sh
# SPDX-License-Identifier: MIT
# File: scripts/release-assets.sh
# Purpose: Build deterministic, checksum-rooted GoalRouter release assets

set -eu
umask 077

LC_ALL=C
export LC_ALL

script_directory=$(CDPATH='' cd -P -- "$(dirname -- "$0")" && pwd -P) || {
    printf '%s\n' 'goalrouter release assets: cannot resolve source directory' >&2
    exit 1
}
GOALROUTER_RELEASE_SOURCE_ROOT=${script_directory%/scripts}
export GOALROUTER_RELEASE_SOURCE_ROOT

exec python - "$@" <<'PY'
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import time
import tomllib
import zipfile
from pathlib import Path
from typing import NoReturn

VERSION = "1.0.10"
PROTOCOL_VERSION = 1
IMAGE = "ghcr.io/vparla/goalrouter:1.0.10"
ARCHITECTURES = ["linux/amd64", "linux/arm64"]
MINIMUM_HOSTS = {
    "windows": "10.0.19045",
    "powershell": "5.1",
    "wsl": "2.2.3",
    "docker": "20.10",
}
UNIX_MEMBERS = {
    "goalrouter": "scripts/goalrouter",
    "install.sh": "scripts/install.sh",
    "uninstall.sh": "scripts/uninstall.sh",
}
WINDOWS_MEMBERS = {
    "goalrouter.cmd": "scripts/goalrouter.cmd",
    "goalrouter.ps1": "scripts/goalrouter.ps1",
    "install.ps1": "scripts/install.ps1",
    "uninstall.ps1": "scripts/uninstall.ps1",
}
RAW_ASSETS = {
    "install.ps1": "scripts/install.ps1",
    "install.sh": "scripts/install.sh",
    "uninstall.ps1": "scripts/uninstall.ps1",
    "uninstall.sh": "scripts/uninstall.sh",
}
CHECKSUM_ORDER = [
    "goalrouter-1.0.10-unix.tar.gz",
    "goalrouter-1.0.10-windows.zip",
    "install.ps1",
    "install.sh",
    "release-manifest.json",
    "uninstall.ps1",
    "uninstall.sh",
]
OPTIONS = {
    "--version",
    "--tag",
    "--image",
    "--image-digest",
    "--source-revision",
    "--source-date-epoch",
    "--output-dir",
}


def fail(message: str) -> NoReturn:
    raise ValueError(message)


def parse_arguments(arguments: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option not in OPTIONS:
            fail(f"unknown option: {option}")
        if option in values:
            fail(f"duplicate option: {option}")
        index += 1
        if index >= len(arguments) or arguments[index] in OPTIONS:
            fail(f"missing value for {option}")
        values[option] = arguments[index]
        index += 1
    missing = sorted(OPTIONS - values.keys())
    if missing:
        fail(f"missing required option: {missing[0]}")
    return values


def exact_match(pattern: str, value: str, label: str) -> None:
    if re.fullmatch(pattern, value, flags=re.ASCII) is None:
        fail(f"invalid {label}")


def read_regular_source(root: Path, relative: str) -> bytes:
    path = root / relative
    try:
        metadata = path.lstat()
    except OSError as error:
        fail(f"required source is unavailable: {relative}: {error.strerror}")
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        fail(f"required source is not a regular file: {relative}")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        fail(f"required source escapes the source root: {relative}")
    return path.read_bytes()


def one_regex_value(source: str, pattern: str, label: str) -> str:
    matches = re.findall(pattern, source, flags=re.MULTILINE)
    if len(matches) != 1:
        fail(f"cannot determine exact {label}")
    return matches[0]


def validate_version_surfaces(root: Path, version: str) -> None:
    pyproject = tomllib.loads(read_regular_source(root, "pyproject.toml").decode("utf-8"))
    surfaces = {
        "pyproject.toml": pyproject.get("project", {}).get("version"),
        "src/goalrouter/__init__.py": one_regex_value(
            read_regular_source(root, "src/goalrouter/__init__.py").decode("utf-8"),
            r'^__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"$',
            "package version",
        ),
        "Dockerfile": one_regex_value(
            read_regular_source(root, "Dockerfile").decode("utf-8"),
            r"^ARG VERSION=([0-9]+\.[0-9]+\.[0-9]+)$",
            "Dockerfile runtime version",
        ),
        "scripts/install.ps1": one_regex_value(
            read_regular_source(root, "scripts/install.ps1").decode("utf-8"),
            r"\[string\]\$Version = '([0-9]+\.[0-9]+\.[0-9]+)'",
            "Windows installer version",
        ),
    }
    drift = [name for name, actual in surfaces.items() if actual != version]
    if drift:
        fail(f"version surface drift: {', '.join(sorted(drift))}")
    powershell_installer = read_regular_source(root, "scripts/install.ps1").decode("utf-8")
    protocol = one_regex_value(
        powershell_installer,
        r"^\$script:GoalRouterProtocolVersion = ([0-9]+)$",
        "launcher protocol version",
    )
    runtime_protocol = one_regex_value(
        read_regular_source(root, "src/goalrouter/build_info.py").decode("utf-8"),
        r"^        protocol_version=([0-9]+),$",
        "runtime protocol version",
    )
    if {protocol, runtime_protocol} != {str(PROTOCOL_VERSION)}:
        fail("protocol version drift")


def validate_output(output_text: str, root: Path) -> tuple[Path, int]:
    output = Path(output_text)
    if not output.is_absolute():
        fail("output directory must be absolute")
    if output == Path(output.anchor):
        fail("output directory cannot be a filesystem root")
    try:
        metadata = output.lstat()
    except OSError:
        fail("output directory must already exist")
    if output.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        fail("output directory must be a physical directory")
    try:
        resolved = output.resolve(strict=True)
    except OSError:
        fail("output directory cannot be resolved")
    if resolved != output:
        fail("output directory cannot contain symbolic links")
    try:
        root.relative_to(output)
    except ValueError:
        pass
    else:
        fail("output directory cannot contain the source tree")
    try:
        output.relative_to(root)
    except ValueError:
        pass
    else:
        fail("output directory cannot be inside the source tree")
    try:
        if any(output.iterdir()):
            fail("output directory must be empty")
    except OSError:
        fail("output directory cannot be inspected")
    if not os.access(output.parent, os.W_OK):
        fail("output directory parent is not writable")
    return output, stat.S_IMODE(metadata.st_mode)


def write_regular(path: Path, content: bytes, mode: int) -> None:
    path.write_bytes(content)
    path.chmod(mode)


def build_tar(path: Path, root: Path, epoch: int) -> None:
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch, compresslevel=9) as zipped:
            with tarfile.open(fileobj=zipped, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                for name in sorted(UNIX_MEMBERS):
                    content = read_regular_source(root, UNIX_MEMBERS[name])
                    member = tarfile.TarInfo(name=name)
                    member.size = len(content)
                    member.mode = 0o755
                    member.mtime = epoch
                    member.uid = 0
                    member.gid = 0
                    member.uname = ""
                    member.gname = ""
                    archive.addfile(member, fileobj=__import__("io").BytesIO(content))
    path.chmod(0o644)


def build_zip(path: Path, root: Path, epoch: int) -> None:
    timestamp = time.gmtime(epoch)[:6]
    with zipfile.ZipFile(
        path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for name in sorted(WINDOWS_MEMBERS):
            info = zipfile.ZipInfo(filename=name, date_time=timestamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o755) << 16
            info.extra = b""
            info.comment = b""
            archive.writestr(
                info,
                read_regular_source(root, WINDOWS_MEMBERS[name]),
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )
    path.chmod(0o644)


def build_tree(staging: Path, root: Path, values: dict[str, str], epoch: int) -> None:
    build_tar(staging / f"goalrouter-{VERSION}-unix.tar.gz", root, epoch)
    build_zip(staging / f"goalrouter-{VERSION}-windows.zip", root, epoch)
    for name in sorted(RAW_ASSETS):
        write_regular(staging / name, read_regular_source(root, RAW_ASSETS[name]), 0o644)
    manifest = {
        "version": VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "image": IMAGE,
        "image_digest": values["--image-digest"],
        "architectures": ARCHITECTURES,
        "source_revision": values["--source-revision"],
        "minimum_hosts": MINIMUM_HOSTS,
    }
    manifest_bytes = json.dumps(
        manifest,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    write_regular(staging / "release-manifest.json", manifest_bytes, 0o644)
    checksum_lines = []
    for name in CHECKSUM_ORDER:
        digest = hashlib.sha256((staging / name).read_bytes()).hexdigest()
        checksum_lines.append(f"{digest}  {name}\n")
    write_regular(staging / "SHA256SUMS", "".join(checksum_lines).encode("ascii"), 0o644)


def main(arguments: list[str]) -> None:
    values = parse_arguments(arguments)
    if values["--version"] != VERSION:
        fail(f"version must be exactly {VERSION}")
    exact_match(r"v[0-9]+\.[0-9]+\.[0-9]+", values["--tag"], "release tag")
    if values["--tag"] != f"v{VERSION}":
        fail("release tag does not match version")
    if values["--image"] != IMAGE:
        fail(f"image must be exactly {IMAGE}")
    exact_match(r"sha256:[0-9a-f]{64}", values["--image-digest"], "image digest")
    exact_match(r"[0-9a-f]{40}", values["--source-revision"], "source revision")
    exact_match(r"[0-9]+", values["--source-date-epoch"], "SOURCE_DATE_EPOCH")
    epoch = int(values["--source-date-epoch"])
    if not 315_532_800 <= epoch <= 4_294_967_295:
        fail("SOURCE_DATE_EPOCH is outside the deterministic archive timestamp range")

    root_text = os.environ.get("GOALROUTER_RELEASE_SOURCE_ROOT", "")
    root = Path(root_text)
    if not root.is_absolute() or root.resolve(strict=True) != root:
        fail("source root is not a physical absolute directory")
    validate_version_surfaces(root, VERSION)
    output, output_mode = validate_output(values["--output-dir"], root)

    staging_text = tempfile.mkdtemp(prefix=".goalrouter-release-", dir=output.parent)
    staging = Path(staging_text)
    removed_output = False
    try:
        build_tree(staging, root, values, epoch)
        expected = {"SHA256SUMS", *CHECKSUM_ORDER}
        actual = {path.name for path in staging.iterdir()}
        if actual != expected or any(path.is_symlink() or not path.is_file() for path in staging.iterdir()):
            fail("staged release tree is incomplete or unsafe")
        staging.chmod(output_mode)
        output.rmdir()
        removed_output = True
        os.replace(staging, output)
        removed_output = False
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        if removed_output and not output.exists():
            previous_umask = os.umask(0)
            try:
                output.mkdir(mode=output_mode)
            finally:
                os.umask(previous_umask)
        raise


try:
    main(sys.argv[1:])
except (OSError, UnicodeError, ValueError, KeyError, TypeError, tomllib.TOMLDecodeError) as error:
    print(f"goalrouter release assets: {error}", file=sys.stderr)
    raise SystemExit(1) from None
PY
