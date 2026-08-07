# SPDX-License-Identifier: MIT
# File: tests/distribution/test_docker_cleanup_helper.py
# Purpose: Directly enforce shared label-owned Docker cleanup semantics

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Final

HELPER: Final = Path("scripts/docker-resource-cleanup.sh").resolve()
FAKE_DOCKER: Final = Path(
    "tests/fixtures/distribution/fake-cleanup-docker"
).resolve()


def add_resource(
    root: Path,
    kind: str,
    name: str,
    *,
    owner: str = "test-owner",
    run: str = "test-run",
    image_id: str | None = None,
) -> None:
    resource = root / kind / name
    resource.mkdir(parents=True)
    (resource / "owner").write_text(owner, encoding="utf-8")
    (resource / "run").write_text(run, encoding="utf-8")
    if image_id is not None:
        (resource / "id").write_text(image_id, encoding="utf-8")


def run_helper(root: Path, body: str) -> subprocess.CompletedProcess[bytes]:
    fake_bin = root / "bin"
    fake_bin.mkdir()
    (fake_bin / "docker").symlink_to(FAKE_DOCKER)
    environment = {
        "FAKE_DOCKER_ROOT": str(root),
        "PATH": f"{fake_bin}:/usr/local/bin:/usr/bin:/bin",
    }
    return subprocess.run(
        ["/bin/sh", "-eu", "-c", f'. "{HELPER}"\n{body}'],
        env=environment,
        capture_output=True,
        check=False,
    )


def test_cleanup_orders_resources_and_is_idempotent(tmp_path: Path) -> None:
    add_resource(tmp_path, "container", "container-one")
    add_resource(tmp_path, "volume", "volume-one")
    add_resource(
        tmp_path, "image", "owned", image_id="sha256:owned-image"
    )

    result = run_helper(
        tmp_path,
        """
gr_cleanup_init test-owner test-run 'helper cleanup'
gr_cleanup_register_image owned-image:local sha256:owned-image
gr_cleanup_owned_resources
gr_cleanup_owned_resources
""",
    )

    assert result.returncode == 0
    events = (tmp_path / "events").read_text(encoding="utf-8").splitlines()
    removals = [event for event in events if event.endswith(" rm")]
    assert removals == ["container rm", "volume rm", "image rm"]
    assert not (tmp_path / "container" / "container-one").exists()
    assert not (tmp_path / "volume" / "volume-one").exists()
    assert not (tmp_path / "image" / "owned").exists()


def test_cleanup_refuses_both_label_mismatches_and_aggregates(tmp_path: Path) -> None:
    add_resource(tmp_path, "container", "wrong-owner", owner="someone-else")
    add_resource(tmp_path, "container", "wrong-run", run="another-run")
    add_resource(tmp_path, "volume", "volume-one")
    add_resource(
        tmp_path, "image", "owned", image_id="sha256:owned-image"
    )

    result = run_helper(
        tmp_path,
        """
gr_cleanup_init test-owner test-run 'helper cleanup'
gr_cleanup_register_image owned-image:local sha256:owned-image
gr_cleanup_owned_resources
""",
    )

    assert result.returncode != 0
    assert result.stderr.count(b"refusing mismatched container") == 2
    events = (tmp_path / "events").read_text(encoding="utf-8").splitlines()
    assert "container rm" not in events
    assert "volume rm" in events
    assert "image rm" in events


def test_signal_cleanup_preserves_signal_status(tmp_path: Path) -> None:
    add_resource(tmp_path, "container", "container-one")
    add_resource(tmp_path, "volume", "volume-one")

    result = run_helper(
        tmp_path,
        """
gr_cleanup_init test-owner test-run 'helper cleanup'
gr_install_cleanup_traps
kill -TERM $$
exit 99
""",
    )

    assert result.returncode == 143
    assert not (tmp_path / "container" / "container-one").exists()
    assert not (tmp_path / "volume" / "volume-one").exists()


def test_smokes_source_helper_relative_to_installed_script() -> None:
    for script_name in (
        "posix-launcher-smoke.sh",
        "posix-launcher-smoke-safety.sh",
        "runtime-image-smoke.sh",
    ):
        script = Path("scripts", script_name).read_text(encoding="utf-8")
        assert 'script_directory=${0%/*}' in script
        assert '. "$script_directory/docker-resource-cleanup.sh"' in script

    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "scripts/docker-resource-cleanup.sh" in dockerfile
    assert os.access(FAKE_DOCKER, os.X_OK)
