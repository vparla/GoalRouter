# SPDX-License-Identifier: MIT
# File: tests/distribution/test_workflow_policy.py
# Purpose: Enforce least-privilege CI and bounded dependency maintenance

import copy
import re
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CI_PATH = ROOT / ".github" / "workflows" / "ci.yml"
DEPENDABOT_PATH = ROOT / ".github" / "dependabot.yml"
PUBLISH_PATH = ROOT / ".github" / "workflows" / "publish.yml"
REQUIRED_GATES = (
    "test",
    "lint",
    "typecheck",
    "package",
    "shellcheck",
    "powershell-test",
    "distribution-test",
)
FORBIDDEN_PROFILES = (
    "live",
    "live-inventory",
    "live-test",
    "validation",
    "distribution-integration",
    "runtime-smoke",
    "runtime-smoke-interrupt",
    "posix-launcher-smoke",
    "posix-launcher-smoke-safety",
    "posix-installer-smoke",
)
JOB_TIMEOUT_MINUTES = 60
TRUFFLEHOG_ACTION = (
    "trufflesecurity/trufflehog@6f3c981e7b77f235fd2702dd74af25fc4b72bf11"
)
SETUP_BUILDX_ACTION = (
    "docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c"
)
BUILDKIT_IMAGE = (
    "moby/buildkit@sha256:"
    "2f5adac4ecd194d9f8c10b7b5d7bceb5186853db1b26e5abd3a657af0b7e26ec"
)
PUBLISH_JOBS = {
    "release-gates",
    "build-amd64",
    "build-arm64",
    "publish-edge",
    "publish-stable",
}
IMAGE = "ghcr.io/vparla/goalrouter"
GHCR_LOGIN_STEP = {
    "name": "Log in to GHCR",
    "env": {"GHCR_TOKEN": "${{ secrets.GITHUB_TOKEN }}"},
    "run": (
        "printf '%s' \"$GHCR_TOKEN\" | docker login ghcr.io "
        '--username "$GITHUB_ACTOR" --password-stdin'
    ),
}
OCI_LABELS = {
    "org.opencontainers.image.source",
    "org.opencontainers.image.version",
    "org.opencontainers.image.revision",
    "org.opencontainers.image.licenses",
    "org.opencontainers.image.title",
    "org.opencontainers.image.description",
    "org.opencontainers.image.created",
    "org.opencontainers.image.documentation",
}


class _Yaml12SafeLoader(yaml.SafeLoader):
    """Parse workflow keys using YAML 1.2 boolean semantics."""


def _construct_unique_mapping(
    loader: _Yaml12SafeLoader, node: yaml.MappingNode
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=False)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate mapping key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=False)
    return mapping


_Yaml12SafeLoader.yaml_implicit_resolvers = {
    key: [
        resolver
        for resolver in resolvers
        if resolver[0] != "tag:yaml.org,2002:bool"
    ]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Yaml12SafeLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|false)$", re.IGNORECASE),
    list("tTfF"),
)
_Yaml12SafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _load_yaml(path: Path) -> Mapping[str, Any]:
    loaded = yaml.load(path.read_text(encoding="utf-8"), Loader=_Yaml12SafeLoader)
    assert isinstance(loaded, Mapping)
    return loaded


def _walk(value: Any) -> Iterator[Any]:
    yield value
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield from _walk(key)
            yield from _walk(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            yield from _walk(child)


def _mapping_values_for_key(value: Any, expected_key: str) -> Iterator[Any]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key == expected_key:
                yield child
            yield from _mapping_values_for_key(child, expected_key)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            yield from _mapping_values_for_key(child, expected_key)


def _expected_steps() -> list[dict[str, Any]]:
    return [
        {
            "name": "Check out source",
            "uses": "actions/checkout@v6",
            "with": {"persist-credentials": False},
        },
        {
            "name": "Secret scan",
            "if": "github.event_name == 'pull_request' || github.event_name == 'push'",
            "uses": TRUFFLEHOG_ACTION,
            "with": {
                "base": "",
                "head": "HEAD",
                "version": "3.96.0",
                "extra_args": "--results=verified,unknown",
            },
        },
        {
            "name": "Dependency review",
            "if": "github.event_name == 'pull_request'",
            "uses": "actions/dependency-review-action@v4",
        },
        {"name": "Check Dockerfile warnings", "run": "docker compose build --check"},
        {"name": "Build declared verification images", "run": "docker compose build"},
        {"name": "Test", "run": "docker compose run --rm test"},
        {"name": "Lint", "run": "docker compose run --rm lint"},
        {"name": "Type check", "run": "docker compose run --rm typecheck"},
        {"name": "Package", "run": "docker compose run --rm package"},
        {"name": "Shell contracts", "run": "docker compose run --rm shellcheck"},
        {
            "name": "PowerShell contracts",
            "run": "docker compose run --rm powershell-test",
        },
        {
            "name": "Distribution contracts",
            "run": "docker compose run --rm distribution-test",
        },
    ]


def _valid_workflow_topology() -> dict[str, Any]:
    return {
        "jobs": {
            "quality": {
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": JOB_TIMEOUT_MINUTES,
                "steps": _expected_steps(),
            }
        }
    }


def _workflow_steps(workflow: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    jobs = workflow["jobs"]
    assert isinstance(jobs, Mapping)
    assert list(jobs) == ["quality"]
    job = jobs["quality"]
    assert isinstance(job, Mapping)
    assert set(job) == {"runs-on", "timeout-minutes", "steps"}
    assert type(job["timeout-minutes"]) is int
    assert job["timeout-minutes"] == JOB_TIMEOUT_MINUTES
    job_steps = job["steps"]
    assert isinstance(job_steps, list)
    assert all(isinstance(step, Mapping) for step in job_steps)
    steps = list(job_steps)
    assert steps == _expected_steps()
    return steps


def _job_steps(job: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    steps = job["steps"]
    assert isinstance(steps, list)
    assert all(isinstance(step, Mapping) for step in steps)
    return list(steps)


def _named_steps(job: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    steps = _job_steps(job)
    assert all(isinstance(step.get("name"), str) for step in steps)
    by_name = {str(step["name"]): step for step in steps}
    assert len(by_name) == len(steps)
    return by_name


def _expected_native_build_run(platform: str, architecture: str) -> str:
    description_label = (
        '  --label org.opencontainers.image.description="Task-driven model routing '
        'controller for local Codex engineering workflows" \\'
    )
    digest_command = (
        r'''digest=$(sed -n 's/.*"containerimage.digest":[[:space:]]*"'''
        r'''\(sha256:[0-9a-f]\{64\}\)".*/\1/p' "$metadata")'''
    )
    return f"""set -euo pipefail
image=ghcr.io/vparla/goalrouter
version=1.0.10
revision="$GITHUB_SHA"
created=$(git show -s --format=%cI "$GITHUB_SHA")
temporary_tag="$image:tmp-${{GITHUB_SHA:0:12}}-{architecture}"
metadata="$RUNNER_TEMP/build-{architecture}.json"
docker buildx build \\
  --platform {platform} \\
  --target runtime \\
  --build-arg VERSION="$version" \\
  --build-arg REVISION="$revision" \\
  --build-arg CREATED="$created" \\
  --label org.opencontainers.image.source=https://github.com/vparla/GoalRouter \\
  --label org.opencontainers.image.version="$version" \\
  --label org.opencontainers.image.revision="$revision" \\
  --label org.opencontainers.image.licenses=MIT \\
  --label org.opencontainers.image.title=GoalRouter \\
{description_label}
  --label org.opencontainers.image.created="$created" \\
  --label org.opencontainers.image.documentation=https://github.com/vparla/GoalRouter#readme \\
  --sbom=true \\
  --provenance=mode=max \\
  --tag "$temporary_tag" \\
  --metadata-file "$metadata" \\
  --push \\
  .
{digest_command}
[[ "$digest" =~ ^sha256:[0-9a-f]{{64}}$ ]]
printf 'digest=%s\\n' "$digest" >> "$GITHUB_OUTPUT"
"""


def _assert_publish_policy(workflow: Mapping[str, Any]) -> None:
    assert set(workflow) == {"name", "on", "permissions", "concurrency", "jobs"}
    assert workflow["on"] == {
        "push": {"branches": ["main"], "tags": ["v*"]},
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "publish-${{ github.ref }}",
        "cancel-in-progress": False,
    }
    jobs = workflow["jobs"]
    assert isinstance(jobs, Mapping)
    assert set(jobs) == PUBLISH_JOBS

    gates = jobs["release-gates"]
    assert isinstance(gates, Mapping)
    assert set(gates) == {"runs-on", "timeout-minutes", "permissions", "steps"}
    assert gates["runs-on"] == "ubuntu-24.04"
    assert gates["timeout-minutes"] == 60
    assert gates["permissions"] == {"actions": "read", "contents": "read"}
    gate_steps = _named_steps(gates)
    assert list(gate_steps) == [
        "Check out source",
        "Verify stable tag safety",
        "Check Dockerfile warnings",
        "Build declared verification images",
        "Test",
        "Lint",
        "Type check",
        "Package",
        "Shell contracts",
        "PowerShell contracts",
        "Distribution contracts",
        "Validate stable version surfaces",
    ]
    assert gate_steps["Check out source"] == {
        "name": "Check out source",
        "uses": "actions/checkout@v6",
        "with": {"fetch-depth": 0, "persist-credentials": False},
    }
    stable_safety = gate_steps["Verify stable tag safety"]
    assert set(stable_safety) == {"name", "if", "env", "run"}
    assert stable_safety["if"] == "startsWith(github.ref, 'refs/tags/v')"
    assert stable_safety["env"] == {"GH_TOKEN": "${{ secrets.GITHUB_TOKEN }}"}
    safety_run = stable_safety["run"]
    assert isinstance(safety_run, str)
    assert safety_run == "\n".join(
        [
            "set -euo pipefail",
            '[[ "$GITHUB_REF_NAME" =~ ^v[0-9]+\\.[0-9]+\\.[0-9]+$ ]]',
            'test "$(git cat-file -t "$GITHUB_REF")" = tag',
            'test "$(git rev-parse "$GITHUB_REF^{}")" = "$GITHUB_SHA"',
            "git fetch --no-tags origin main:refs/remotes/origin/main",
            'git merge-base --is-ancestor "$GITHUB_SHA" refs/remotes/origin/main',
            "successful_runs=$(gh api --method GET \\",
            '  "repos/${GITHUB_REPOSITORY}/actions/workflows/ci.yml/runs" \\',
            '  -f head_sha="$GITHUB_SHA" \\',
            "  -f branch=main \\",
            "  -f event=push \\",
            "  -f status=success \\",
            "  --jq '.total_count')",
            'test "$successful_runs" -ge 1',
            "",
        ]
    )
    for required in (
        "git cat-file -t",
        "git rev-parse",
        "git merge-base --is-ancestor",
        "actions/workflows/ci.yml/runs",
        "head_sha",
        "branch=main",
        "status=success",
        "^v[0-9]+\\.[0-9]+\\.[0-9]+$",
    ):
        assert required in safety_run
    exact_gate_commands = {
        "Check Dockerfile warnings": "docker compose build --check",
        "Build declared verification images": "docker compose build",
        "Test": "docker compose run --rm test",
        "Lint": "docker compose run --rm lint",
        "Type check": "docker compose run --rm typecheck",
        "Package": "docker compose run --rm package",
        "Shell contracts": "docker compose run --rm shellcheck",
        "PowerShell contracts": "docker compose run --rm powershell-test",
        "Distribution contracts": "docker compose run --rm distribution-test",
    }
    for name, command in exact_gate_commands.items():
        assert gate_steps[name] == {"name": name, "run": command}
    assert set(gate_steps["Validate stable version surfaces"]) == {"name", "if", "run"}
    assert gate_steps["Validate stable version surfaces"]["if"] == (
        "startsWith(github.ref, 'refs/tags/v')"
    )
    version_run = gate_steps["Validate stable version surfaces"]["run"]
    assert isinstance(version_run, str)
    assert version_run == "\n".join(
        [
            "set -euo pipefail",
            'parent="$RUNNER_TEMP/release-version-check"',
            'mkdir -p "$parent/assets"',
            'source_date_epoch=$(git show -s --format=%ct "$GITHUB_SHA")',
            "docker compose run --rm --no-deps \\",
            '  -v "$parent:/release" \\',
            "  release-assets \\",
            "  --version 1.0.10 \\",
            '  --tag "$GITHUB_REF_NAME" \\',
            "  --image ghcr.io/vparla/goalrouter:1.0.10 \\",
            '  --image-digest "sha256:' + ("0" * 64) + '" \\',
            '  --source-revision "$GITHUB_SHA" \\',
            '  --source-date-epoch "$source_date_epoch" \\',
            "  --output-dir /release/assets",
            "",
        ]
    )
    assert "docker compose run --rm --no-deps" in version_run
    assert "release-assets" in version_run
    assert "--tag \"$GITHUB_REF_NAME\"" in version_run

    build_expectations = {
        "build-amd64": ("ubuntu-24.04", "linux/amd64", "amd64"),
        "build-arm64": ("ubuntu-24.04-arm", "linux/arm64", "arm64"),
    }
    for job_name, (runner, platform, architecture) in build_expectations.items():
        job = jobs[job_name]
        assert isinstance(job, Mapping)
        assert set(job) == {
            "needs",
            "runs-on",
            "timeout-minutes",
            "permissions",
            "outputs",
            "steps",
        }
        assert job["needs"] == "release-gates"
        assert job["runs-on"] == runner
        assert job["timeout-minutes"] == 60
        assert job["permissions"] == {"contents": "read", "packages": "write"}
        assert job["outputs"] == {"digest": "${{ steps.build.outputs.digest }}"}
        steps = _named_steps(job)
        assert list(steps) == [
            "Check out source",
            "Set up Docker Buildx",
            "Log in to GHCR",
            "Build native image",
        ]
        assert steps["Check out source"] == {
            "name": "Check out source",
            "uses": "actions/checkout@v6",
            "with": {"fetch-depth": 0, "persist-credentials": False},
        }
        assert steps["Set up Docker Buildx"] == {
            "name": "Set up Docker Buildx",
            "uses": SETUP_BUILDX_ACTION,
            "with": {
                "cache-binary": False,
                "cleanup": True,
                "driver": "docker-container",
                "driver-opts": f"image={BUILDKIT_IMAGE}",
                "keep-state": False,
                "use": True,
            },
        }
        login = steps["Log in to GHCR"]
        assert login == GHCR_LOGIN_STEP
        build = steps["Build native image"]
        assert set(build) == {"name", "id", "run"}
        assert build["id"] == "build"
        command = build["run"]
        assert isinstance(command, str)
        assert command == _expected_native_build_run(platform, architecture)
        assert f"--platform {platform}" in command
        assert "docker buildx build" in command
        assert "--target runtime" in command
        assert "--sbom=true" in command
        assert "--provenance=mode=max" in command
        assert "--push" in command
        assert f'temporary_tag="$image:tmp-${{GITHUB_SHA:0:12}}-{architecture}"' in command
        assert "containerimage.digest" in command
        assert "GITHUB_OUTPUT" in command
        for label in OCI_LABELS:
            assert f"--label {label}=" in command
        lowered = command.lower()
        assert "qemu" not in lowered
        assert "--platform linux/amd64,linux/arm64" not in lowered

    final_permissions = {
        "publish-edge": {
            "attestations": "write",
            "contents": "read",
            "id-token": "write",
            "packages": "write",
        },
        "publish-stable": {
            "attestations": "write",
            "contents": "write",
            "id-token": "write",
            "packages": "write",
        },
    }
    final_conditions = {
        "publish-edge": "github.ref == 'refs/heads/main'",
        "publish-stable": "startsWith(github.ref, 'refs/tags/v')",
    }
    for job_name in ("publish-edge", "publish-stable"):
        job = jobs[job_name]
        assert isinstance(job, Mapping)
        expected_keys = {
            "if",
            "needs",
            "runs-on",
            "timeout-minutes",
            "permissions",
            "steps",
        }
        if job_name == "publish-stable":
            expected_keys.add("environment")
        assert set(job) == expected_keys
        if job_name == "publish-stable":
            assert job["environment"] == "release"
        assert job["if"] == final_conditions[job_name]
        assert job["needs"] == ["build-amd64", "build-arm64"]
        assert job["runs-on"] == "ubuntu-24.04"
        assert job["timeout-minutes"] == 60
        assert job["permissions"] == final_permissions[job_name]

        steps = _named_steps(job)
        assert steps["Log in to GHCR"] == GHCR_LOGIN_STEP
        attest = steps["Attest image index"]
        assert attest == {
            "name": "Attest image index",
            "uses": "actions/attest@v4",
            "with": {
                "subject-name": IMAGE,
                "subject-digest": "${{ steps.index.outputs.digest }}",
                "push-to-registry": True,
                "create-storage-record": False,
            },
        }

    edge_steps = _named_steps(jobs["publish-edge"])
    assert list(edge_steps) == ["Log in to GHCR", "Publish image index", "Attest image index"]
    edge_publish = edge_steps["Publish image index"]
    assert set(edge_publish) == {"name", "id", "env", "run"}
    assert edge_publish["id"] == "index"
    assert edge_publish["env"] == {"GH_TOKEN": "${{ secrets.GITHUB_TOKEN }}"}
    edge_run = edge_publish["run"]
    assert "repos/${GITHUB_REPOSITORY}/git/ref/heads/main" in edge_run
    assert 'test "$latest_main" = "$GITHUB_SHA"' in edge_run
    assert edge_run.index('test "$latest_main" = "$GITHUB_SHA"') < edge_run.index(
        "docker buildx imagetools create"
    )
    assert "docker buildx imagetools create" in edge_run
    assert "${{ needs.build-amd64.outputs.digest }}" in edge_run
    assert "${{ needs.build-arm64.outputs.digest }}" in edge_run
    assert "docker buildx imagetools inspect" in edge_run
    assert "GITHUB_OUTPUT" in edge_run
    assert re.findall(r'--tag "\$image:([^\"]+)"', edge_run) == [
        "edge",
        "sha-${GITHUB_SHA:0:12}",
    ]

    stable_steps = _named_steps(jobs["publish-stable"])
    assert list(stable_steps) == [
        "Check out source",
        "Log in to GHCR",
        "Verify immutable release is absent",
        "Prepare image index",
        "Build release assets",
        "Verify release assets",
        "Publish temporary image index",
        "Attest image index",
        "Revalidate stable publication preconditions",
        "Publish stable image aliases",
        "Create GitHub Release",
    ]
    assert stable_steps["Check out source"] == {
        "name": "Check out source",
        "uses": "actions/checkout@v6",
        "with": {"fetch-depth": 0, "persist-credentials": False},
    }
    immutable_guard = stable_steps["Verify immutable release is absent"]
    assert set(immutable_guard) == {"name", "env", "run"}
    assert immutable_guard["env"] == {"GH_TOKEN": "${{ secrets.GITHUB_TOKEN }}"}
    guard_run = immutable_guard["run"]
    assert isinstance(guard_run, str)
    assert "https://ghcr.io/token?scope=repository:vparla/goalrouter:pull" in guard_run
    for media_type in (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ):
        assert guard_run.count(media_type) == 2
    assert 'test "$GITHUB_REF_NAME" = v1.0.10' in guard_run
    assert "for tag in v1.0.10 1.0.10; do" in guard_run
    assert "for tag in v1.0.10 1.0.10 1.0" not in guard_run
    assert "https://ghcr.io/v2/vparla/goalrouter/manifests/$tag" in guard_run
    assert '"200")' in guard_run
    assert '"404")' in guard_run
    assert "https://api.github.com/repos/${GITHUB_REPOSITORY}/releases/tags/$GITHUB_REF_NAME" in (
        guard_run
    )
    assert "immutable GHCR tag already exists" in guard_run
    assert "GitHub Release already exists" in guard_run
    assert "resolve_existing_digest()" in guard_run
    assert "prior_digest=$(resolve_existing_digest 1.0.9 prior-immutable)" in guard_run
    assert "for tag in 1.0 1 latest; do" in guard_run
    assert 'moving_digest=$(resolve_existing_digest "$tag" moving)' in guard_run
    assert 'test "$moving_digest" = "$prior_digest"' in guard_run
    assert "GHCR alias is missing" in guard_run
    assert "moving GHCR alias diverged from 1.0.9" in guard_run
    assert "GHCR alias returned multiple digests" in guard_run
    assert (
        '  case "$status" in\n'
        "    \"200\") printf 'immutable GHCR tag already exists: %s\\n' \"$tag\" >&2; "
        "exit 1 ;;\n"
        '    "404") ;;\n'
        "    *) printf 'unexpected GHCR status for %s: %s\\n' \"$tag\" \"$status\" >&2; "
        "exit 1 ;;\n"
        "  esac"
    ) in guard_run
    assert (
        'case "$release_status" in\n'
        "  \"200\") printf 'GitHub Release already exists: %s\\n' "
        '"$GITHUB_REF_NAME" >&2; exit 1 ;;\n'
        '  "404") ;;\n'
        "  *) printf 'unexpected GitHub Release status: %s\\n' \"$release_status\" >&2; "
        "exit 1 ;;\n"
        "esac"
    ) in guard_run
    final_guard = stable_steps["Revalidate stable publication preconditions"]
    assert set(final_guard) == {"name", "env", "run"}
    assert final_guard["env"] == {"GH_TOKEN": "${{ secrets.GITHUB_TOKEN }}"}
    assert final_guard["run"] == guard_run
    stable_order = list(stable_steps)
    assert stable_order.index("Revalidate stable publication preconditions") + 1 == (
        stable_order.index("Publish stable image aliases")
    )
    prepare = stable_steps["Prepare image index"]
    assert set(prepare) == {"name", "id", "run"}
    assert prepare["id"] == "index"
    prepare_run = prepare["run"]
    assert "docker buildx imagetools create" in prepare_run
    assert "--dry-run" in prepare_run
    assert "--metadata-file" not in prepare_run
    assert "--tag" not in prepare_run
    assert "${{ needs.build-amd64.outputs.digest }}" in prepare_run
    assert "${{ needs.build-arm64.outputs.digest }}" in prepare_run
    assert "tail -c 1" in prepare_run
    assert "od -An -tx1" in prepare_run
    assert "head -c -1" in prepare_run
    assert "sha256sum" in prepare_run
    assert "GITHUB_OUTPUT" in prepare_run
    temporary_publish = stable_steps["Publish temporary image index"]
    assert set(temporary_publish) == {"name", "run"}
    temporary_publish_run = temporary_publish["run"]
    assert re.findall(r'--tag "\$image:([^\"]+)"', temporary_publish_run) == [
        "tmp-${GITHUB_SHA:0:12}-index",
    ]
    assert "--metadata-file" in temporary_publish_run
    assert "${{ needs.build-amd64.outputs.digest }}" in temporary_publish_run
    assert "${{ needs.build-arm64.outputs.digest }}" in temporary_publish_run
    assert "containerimage.descriptor" in temporary_publish_run
    assert 'test "$published_digest" = "${{ steps.index.outputs.digest }}"' in (
        temporary_publish_run
    )
    stable_aliases = stable_steps["Publish stable image aliases"]
    assert set(stable_aliases) == {"name", "run"}
    stable_aliases_run = stable_aliases["run"]
    assert re.findall(r'--tag "\$image:([^\"]+)"', stable_aliases_run) == [
        "v1.0.10",
        "1.0.10",
        "1.0",
        "1",
        "latest",
    ]
    assert "--metadata-file" in stable_aliases_run
    assert '"$image@${{ steps.index.outputs.digest }}"' in stable_aliases_run
    assert "needs.build-amd64.outputs.digest" not in stable_aliases_run
    assert "needs.build-arm64.outputs.digest" not in stable_aliases_run
    assert "containerimage.descriptor" in stable_aliases_run
    assert 'test "$aliased_digest" = "${{ steps.index.outputs.digest }}"' in stable_aliases_run
    assert "for tag in v1.0.10 1.0.10 1.0 1 latest; do" in stable_aliases_run
    assert "docker buildx imagetools inspect" in stable_aliases_run
    assert '"$image:$tag"' in stable_aliases_run
    assert "--format '{{json .Manifest}}'" in stable_aliases_run
    assert 'test "$published_alias_digest" = "${{ steps.index.outputs.digest }}"' in (
        stable_aliases_run
    )
    assets_run = stable_steps["Build release assets"]["run"]
    assert set(stable_steps["Build release assets"]) == {"name", "run"}
    assert "docker compose build release-assets" in assets_run
    assert "docker compose run --rm --no-deps" in assets_run
    assert "--image-digest \"${{ steps.index.outputs.digest }}\"" in assets_run
    assert "--source-revision \"$GITHUB_SHA\"" in assets_run
    assert "--source-date-epoch \"$source_date_epoch\"" in assets_run
    assert "--tag \"$GITHUB_REF_NAME\"" in assets_run
    assert set(stable_steps["Verify release assets"]) == {"name", "run"}
    assert stable_steps["Verify release assets"]["run"] == (
        'cd "$RUNNER_TEMP/release-parent/assets"\nsha256sum -c SHA256SUMS\n'
    )
    assert set(stable_steps["Attest image index"]) == {"name", "uses", "with"}
    release = stable_steps["Create GitHub Release"]
    assert set(release) == {"name", "env", "run"}
    assert release["env"] == {"GH_TOKEN": "${{ secrets.GITHUB_TOKEN }}"}
    release_run = release["run"]
    assert isinstance(release_run, str)
    assert "gh release create \"$GITHUB_REF_NAME\"" in release_run
    assert "--generate-notes" in release_run
    expected_assets = [
        "SHA256SUMS",
        "goalrouter-1.0.10-unix.tar.gz",
        "goalrouter-1.0.10-windows.zip",
        "install.ps1",
        "install.sh",
        "release-manifest.json",
        "uninstall.ps1",
        "uninstall.sh",
    ]
    included_assets = [name for name in expected_assets if f'"$assets/{name}"' in release_run]
    assert included_assets == expected_assets
    assert release_run.count('"$assets/') == 8

    uses_values = list(_mapping_values_for_key(workflow, "uses"))
    assert set(uses_values) == {
        "actions/checkout@v6",
        "actions/attest@v4",
        SETUP_BUILDX_ACTION,
    }
    for uses in uses_values:
        assert isinstance(uses, str)
        if uses not in {"actions/checkout@v6", "actions/attest@v4"}:
            assert re.search(r"@[0-9a-f]{40}$", uses)

    scalar_values = [value for value in _walk(workflow) if isinstance(value, str)]
    lowered = [value.lower() for value in scalar_values]
    assert not any("pull_request" in value or "workflow_dispatch" in value for value in lowered)
    assert not any("upload-artifact" in value for value in lowered)
    assert not any(
        "secrets." in value and value != "${{ secrets.github_token }}" for value in lowered
    )
    assert not any("pat" in value or "password=" in value for value in lowered)


def _dependabot_updates(document: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    assert set(document) == {"version", "updates"}
    assert document["version"] == 2
    updates = document["updates"]
    assert isinstance(updates, list)
    assert len(updates) == 2
    assert all(isinstance(update, Mapping) for update in updates)
    by_ecosystem = {update["package-ecosystem"]: update for update in updates}
    assert set(by_ecosystem) == {"github-actions", "pip"}

    expected_intervals = {"github-actions": "weekly", "pip": "monthly"}
    common_keys = {
        "package-ecosystem",
        "directory",
        "schedule",
        "open-pull-requests-limit",
        "groups",
    }
    for ecosystem, update in by_ecosystem.items():
        expected_keys = common_keys | ({"ignore"} if ecosystem == "pip" else set())
        assert set(update) == expected_keys
        assert update["directory"] == "/"
        assert update["schedule"] == {"interval": expected_intervals[ecosystem]}
        limit = update["open-pull-requests-limit"]
        assert type(limit) is int
        assert 1 <= limit <= 5
        assert update["groups"] == {
            "minor-and-patch": {
                "patterns": ["*"],
                "update-types": ["minor", "patch"],
            }
        }
    assert "ignore" not in by_ecosystem["github-actions"]
    ignore = by_ecosystem["pip"]["ignore"]
    assert isinstance(ignore, list)
    assert len(ignore) == 1
    assert isinstance(ignore[0], Mapping)
    assert set(ignore[0]) == {"dependency-name", "versions"}
    return by_ecosystem


def test_complete_workflow_walk_descends_into_sequences() -> None:
    nested = {"jobs": [{"steps": [{"with": {"token": "secret-value"}}]}]}
    assert "secret-value" in _walk(nested)


def test_serial_topology_rejects_commands_split_across_jobs() -> None:
    workflow = {
        "jobs": {
            "first": {
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 60,
                "steps": [
                    {"name": "checkout", "uses": "actions/checkout@v6"},
                    {"name": "build", "run": "build"},
                ],
            },
            "second": {
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 60,
                "steps": [{"name": "test", "run": "test"}],
            },
        }
    }
    with pytest.raises(AssertionError):
        _workflow_steps(workflow)


def test_serial_topology_requires_checkout_as_the_first_step() -> None:
    workflow = {
        "jobs": {
            "quality": {
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 60,
                "steps": [
                    {"name": "build", "run": "build"},
                    {"name": "checkout", "uses": "actions/checkout@v6"},
                ],
            }
        }
    }
    with pytest.raises(AssertionError):
        _workflow_steps(workflow)


def test_ci_rejects_timeout_and_step_name_mutations() -> None:
    timeout_workflow = _valid_workflow_topology()
    timeout_job = timeout_workflow["jobs"]["quality"]
    timeout_job["timeout-minutes"] = 1
    with pytest.raises(AssertionError):
        _workflow_steps(timeout_workflow)

    renamed_workflow = _valid_workflow_topology()
    renamed_steps = renamed_workflow["jobs"]["quality"]["steps"]
    renamed_steps[4]["name"] = "Renamed build"
    with pytest.raises(AssertionError):
        _workflow_steps(renamed_workflow)


def test_ci_rejects_omitted_or_non_failing_build_check() -> None:
    omitted_workflow = _valid_workflow_topology()
    omitted_steps = omitted_workflow["jobs"]["quality"]["steps"]
    del omitted_steps[3]
    with pytest.raises(AssertionError):
        _workflow_steps(omitted_workflow)

    non_failing_workflow = _valid_workflow_topology()
    check_step = non_failing_workflow["jobs"]["quality"]["steps"][3]
    check_step["continue-on-error"] = True
    with pytest.raises(AssertionError):
        _workflow_steps(non_failing_workflow)


def test_ci_rejects_missing_or_unscoped_security_actions() -> None:
    missing_secret_scan = _valid_workflow_topology()
    del missing_secret_scan["jobs"]["quality"]["steps"][1]
    with pytest.raises(AssertionError):
        _workflow_steps(missing_secret_scan)

    unscoped_dependency_review = _valid_workflow_topology()
    del unscoped_dependency_review["jobs"]["quality"]["steps"][2]["if"]
    with pytest.raises(AssertionError):
        _workflow_steps(unscoped_dependency_review)


def test_ci_rejects_floating_secret_scanner_action() -> None:
    workflow = _valid_workflow_topology()
    workflow["jobs"]["quality"]["steps"][1]["uses"] = "trufflesecurity/trufflehog@main"
    with pytest.raises(AssertionError):
        _workflow_steps(workflow)


def test_yaml_loader_rejects_duplicate_mapping_keys(tmp_path: Path) -> None:
    duplicate_yaml = tmp_path / "duplicate.yml"
    duplicate_yaml.write_text("name: first\nname: second\n", encoding="utf-8")
    with pytest.raises(yaml.constructor.ConstructorError, match="duplicate mapping key"):
        _load_yaml(duplicate_yaml)


def test_publish_workflow_is_native_deterministic_and_least_privilege() -> None:
    _assert_publish_policy(_load_yaml(PUBLISH_PATH))


def test_publish_policy_rejects_permission_event_and_environment_mutations() -> None:
    workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
    workflow["on"]["workflow_dispatch"] = {}
    with pytest.raises(AssertionError):
        _assert_publish_policy(workflow)

    workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
    stable_steps = workflow["jobs"]["publish-stable"]["steps"]
    guard = next(
        step for step in stable_steps if step["name"] == "Verify immutable release is absent"
    )
    release_case = (
        'case "$release_status" in\n'
        "  \"200\") printf 'GitHub Release already exists: %s\\n' "
        '"$GITHUB_REF_NAME" >&2; exit 1 ;;\n'
        '  "404") ;;'
    )
    guard["run"] = guard["run"].replace(
        release_case,
        release_case.replace('"404")', '"403")'),
    )
    with pytest.raises(AssertionError):
        _assert_publish_policy(workflow)

    workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
    workflow["jobs"]["build-amd64"]["permissions"]["id-token"] = "write"
    with pytest.raises(AssertionError):
        _assert_publish_policy(workflow)

    workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
    del workflow["jobs"]["publish-stable"]["environment"]
    with pytest.raises(AssertionError):
        _assert_publish_policy(workflow)

    workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
    workflow["concurrency"]["cancel-in-progress"] = True
    with pytest.raises(AssertionError):
        _assert_publish_policy(workflow)


def test_publish_policy_rejects_runner_platform_and_build_attestation_mutations() -> None:
    workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
    workflow["jobs"]["build-arm64"]["runs-on"] = "ubuntu-24.04"
    with pytest.raises(AssertionError):
        _assert_publish_policy(workflow)

    workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
    build = workflow["jobs"]["build-amd64"]["steps"][3]
    build["run"] = build["run"].replace("--sbom=true", "--sbom=false")
    with pytest.raises(AssertionError):
        _assert_publish_policy(workflow)

    workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
    build = workflow["jobs"]["build-arm64"]["steps"][3]
    build["run"] = build["run"].replace("--platform linux/arm64", "--platform linux/amd64")
    with pytest.raises(AssertionError):
        _assert_publish_policy(workflow)


def test_publish_policy_requires_immutable_least_privilege_native_builders() -> None:
    workflow = _load_yaml(PUBLISH_PATH)
    for job_name in ("build-amd64", "build-arm64"):
        setup = workflow["jobs"][job_name]["steps"][1]
        assert setup.get("name") == "Set up Docker Buildx"
        assert setup.get("uses") == SETUP_BUILDX_ACTION
        assert setup.get("with") == {
            "cache-binary": False,
            "cleanup": True,
            "driver": "docker-container",
            "driver-opts": f"image={BUILDKIT_IMAGE}",
            "keep-state": False,
            "use": True,
        }

    for job_name in ("build-amd64", "build-arm64"):
        workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
        setup = workflow["jobs"][job_name]["steps"][1]
        setup["uses"] = "docker/setup-buildx-action@v4"
        with pytest.raises(AssertionError):
            _assert_publish_policy(workflow)

        workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
        setup = workflow["jobs"][job_name]["steps"][1]
        setup["with"]["driver"] = "docker"
        with pytest.raises(AssertionError):
            _assert_publish_policy(workflow)

        workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
        setup = workflow["jobs"][job_name]["steps"][1]
        setup["with"]["driver-opts"] = "image=moby/buildkit:buildx-stable-1"
        with pytest.raises(AssertionError):
            _assert_publish_policy(workflow)

        workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
        setup = workflow["jobs"][job_name]["steps"][1]
        setup["with"]["cache-binary"] = True
        with pytest.raises(AssertionError):
            _assert_publish_policy(workflow)


def test_publish_policy_closes_every_gate_and_native_build_step() -> None:
    for job_name in ("release-gates", "build-amd64", "build-arm64"):
        original = _load_yaml(PUBLISH_PATH)
        steps = original["jobs"][job_name]["steps"]
        for index, step in enumerate(steps):
            workflow = copy.deepcopy(original)
            workflow["jobs"][job_name]["steps"][index]["continue-on-error"] = True
            with pytest.raises(AssertionError):
                _assert_publish_policy(workflow)

            workflow = copy.deepcopy(original)
            workflow["jobs"][job_name]["steps"][index]["if"] = "always()"
            with pytest.raises(AssertionError):
                _assert_publish_policy(workflow)

            if "run" in step:
                workflow = copy.deepcopy(original)
                workflow["jobs"][job_name]["steps"][index]["run"] = "exit 0"
                with pytest.raises(AssertionError):
                    _assert_publish_policy(workflow)


def test_publish_policy_closes_both_finalizer_login_steps() -> None:
    for job_name in ("publish-edge", "publish-stable"):
        original = _load_yaml(PUBLISH_PATH)
        steps = original["jobs"][job_name]["steps"]
        login_index = next(
            index for index, step in enumerate(steps) if step["name"] == "Log in to GHCR"
        )

        workflow = copy.deepcopy(original)
        workflow["jobs"][job_name]["steps"][login_index]["continue-on-error"] = True
        with pytest.raises(AssertionError):
            _assert_publish_policy(workflow)

        workflow = copy.deepcopy(original)
        workflow["jobs"][job_name]["steps"][login_index]["if"] = "always()"
        with pytest.raises(AssertionError):
            _assert_publish_policy(workflow)

        workflow = copy.deepcopy(original)
        workflow["jobs"][job_name]["steps"][login_index]["run"] = "exit 0"
        with pytest.raises(AssertionError):
            _assert_publish_policy(workflow)

        workflow = copy.deepcopy(original)
        login = workflow["jobs"][job_name]["steps"][login_index]
        login["run"] += '\nprintf \'%s\\n\' "$GHCR_TOKEN"'
        with pytest.raises(AssertionError):
            _assert_publish_policy(workflow)


def test_publish_policy_rejects_tag_attestation_release_and_action_mutations() -> None:
    workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
    edge = workflow["jobs"]["publish-edge"]["steps"][1]
    edge["run"] = edge["run"].replace('$image:edge', '$image:latest')
    with pytest.raises(AssertionError):
        _assert_publish_policy(workflow)

    for media_type in (
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ):
        workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
        stable_steps = workflow["jobs"]["publish-stable"]["steps"]
        guard = next(
            step
            for step in stable_steps
            if step["name"] == "Verify immutable release is absent"
        )
        guard["run"] = guard["run"].replace(f", {media_type}", "")
        with pytest.raises(AssertionError):
            _assert_publish_policy(workflow)

    workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
    stable_steps = workflow["jobs"]["publish-stable"]["steps"]
    final_guard_index = next(
        (
            index
            for index, step in enumerate(stable_steps)
            if step["name"] == "Revalidate stable publication preconditions"
        ),
        None,
    )
    assert final_guard_index is not None
    del stable_steps[final_guard_index]
    with pytest.raises(AssertionError):
        _assert_publish_policy(workflow)

    workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
    stable_steps = workflow["jobs"]["publish-stable"]["steps"]
    final_guard_index = next(
        (
            index
            for index, step in enumerate(stable_steps)
            if step["name"] == "Revalidate stable publication preconditions"
        ),
        None,
    )
    assert final_guard_index is not None
    stable_steps[final_guard_index - 1], stable_steps[final_guard_index] = (
        stable_steps[final_guard_index],
        stable_steps[final_guard_index - 1],
    )
    with pytest.raises(AssertionError):
        _assert_publish_policy(workflow)

    workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
    stable_steps = workflow["jobs"]["publish-stable"]["steps"]
    final_guard = next(
        (
            step
            for step in stable_steps
            if step["name"] == "Revalidate stable publication preconditions"
        ),
        None,
    )
    assert final_guard is not None
    final_guard["run"] = final_guard["run"].replace(
        'test "$moving_digest" = "$prior_digest"',
        ': # divergent moving alias accepted during final recheck',
    )
    with pytest.raises(AssertionError):
        _assert_publish_policy(workflow)

    workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
    edge_run = workflow["jobs"]["publish-edge"]["steps"][1]["run"]
    freshness_check = 'test "$latest_main" = "$GITHUB_SHA"'
    workflow["jobs"]["publish-edge"]["steps"][1]["run"] = (
        edge_run.replace(freshness_check, ": # freshness check deferred")
        + f"\n{freshness_check}\n"
    )
    with pytest.raises(AssertionError):
        _assert_publish_policy(workflow)

    workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
    stable_steps = workflow["jobs"]["publish-stable"]["steps"]
    prepare = next(step for step in stable_steps if step["name"] == "Prepare image index")
    prepare["run"] = prepare["run"].replace("--dry-run", "")
    with pytest.raises(AssertionError):
        _assert_publish_policy(workflow)

    workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
    stable_steps = workflow["jobs"]["publish-stable"]["steps"]
    guard = next(
        step for step in stable_steps if step["name"] == "Verify immutable release is absent"
    )
    guard["run"] = guard["run"].replace(
        "for tag in v1.0.10 1.0.10; do", "for tag in v1.0.10; do"
    )
    with pytest.raises(AssertionError):
        _assert_publish_policy(workflow)

    workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
    stable_steps = workflow["jobs"]["publish-stable"]["steps"]
    guard = next(
        step for step in stable_steps if step["name"] == "Verify immutable release is absent"
    )
    guard["run"] = guard["run"].replace(
        '"404") printf \'%s GHCR alias is missing: %s\\n\' "$alias_kind" "$tag" >&2; exit 1 ;;',
        '"404") ;;',
    )
    with pytest.raises(AssertionError):
        _assert_publish_policy(workflow)

    workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
    stable_steps = workflow["jobs"]["publish-stable"]["steps"]
    guard = next(
        step for step in stable_steps if step["name"] == "Verify immutable release is absent"
    )
    guard["run"] = guard["run"].replace(
        'test "$moving_digest" = "$prior_digest"',
        ': # divergent moving alias accepted',
    )
    with pytest.raises(AssertionError):
        _assert_publish_policy(workflow)

    workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
    stable_steps = workflow["jobs"]["publish-stable"]["steps"]
    guard = next(
        step for step in stable_steps if step["name"] == "Verify immutable release is absent"
    )
    guard["run"] = guard["run"].replace(
        '"200") printf \'immutable GHCR tag already exists: %s\\n\' "$tag" >&2; exit 1 ;;',
        '"200") ;;',
    )
    with pytest.raises(AssertionError):
        _assert_publish_policy(workflow)

    workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
    stable_steps = workflow["jobs"]["publish-stable"]["steps"]
    guard = next(
        step for step in stable_steps if step["name"] == "Verify immutable release is absent"
    )
    guard["run"] = guard["run"].replace(
        "*) printf 'unexpected GHCR status for %s: %s\\n' \"$tag\" \"$status\" >&2; exit 1 ;;",
        "*) printf 'unexpected GHCR status for %s: %s\\n' \"$tag\" \"$status\" >&2 ;;",
    )
    with pytest.raises(AssertionError):
        _assert_publish_policy(workflow)

    workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
    stable_steps = workflow["jobs"]["publish-stable"]["steps"]
    publish_index = next(
        index
        for index, step in enumerate(stable_steps)
        if step["name"] == "Publish temporary image index"
    )
    build_assets = next(
        index for index, step in enumerate(stable_steps) if step["name"] == "Build release assets"
    )
    stable_steps[publish_index], stable_steps[build_assets] = (
        stable_steps[build_assets],
        stable_steps[publish_index],
    )
    with pytest.raises(AssertionError):
        _assert_publish_policy(workflow)

    workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
    stable_steps = workflow["jobs"]["publish-stable"]["steps"]
    build_assets_step = next(
        step for step in stable_steps if step["name"] == "Build release assets"
    )
    build_assets_step["continue-on-error"] = True
    with pytest.raises(AssertionError):
        _assert_publish_policy(workflow)

    workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
    stable_steps = workflow["jobs"]["publish-stable"]["steps"]
    publish_step = next(
        step for step in stable_steps if step["name"] == "Publish stable image aliases"
    )
    publish_step["if"] = "always()"
    with pytest.raises(AssertionError):
        _assert_publish_policy(workflow)

    workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
    stable_steps = workflow["jobs"]["publish-stable"]["steps"]
    attest = next(step for step in stable_steps if step["name"] == "Attest image index")
    attest["with"]["push-to-registry"] = False
    with pytest.raises(AssertionError):
        _assert_publish_policy(workflow)

    workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
    stable_steps = workflow["jobs"]["publish-stable"]["steps"]
    release = next(step for step in stable_steps if step["name"] == "Create GitHub Release")
    release["run"] = release["run"].replace('"$assets/uninstall.sh"', '"$assets/extra"')
    with pytest.raises(AssertionError):
        _assert_publish_policy(workflow)

    workflow = copy.deepcopy(_load_yaml(PUBLISH_PATH))
    workflow["jobs"]["build-amd64"]["steps"][0]["uses"] = "actions/checkout@main"
    with pytest.raises(AssertionError):
        _assert_publish_policy(workflow)


def test_ci_triggers_and_permissions_are_exact() -> None:
    workflow = _load_yaml(CI_PATH)
    assert set(workflow) == {"name", "on", "permissions", "jobs"}
    assert workflow["on"] == {
        "pull_request": {},
        "push": {"branches": ["main"]},
        "workflow_dispatch": {},
    }
    assert workflow["permissions"] == {"contents": "read"}

    jobs = workflow["jobs"]
    assert isinstance(jobs, Mapping)
    assert jobs
    for job in jobs.values():
        assert isinstance(job, Mapping)
        assert "permissions" not in job
        assert "environment" not in job
        assert job["runs-on"] == "ubuntu-24.04"


def test_ci_builds_once_then_runs_only_the_seven_serial_compose_gates() -> None:
    workflow = _load_yaml(CI_PATH)
    steps = _workflow_steps(workflow)
    commands = [step["run"] for step in steps if "run" in step]
    assert commands == [
        "docker compose build --check",
        "docker compose build",
        *(f"docker compose run --rm {gate}" for gate in REQUIRED_GATES),
    ]


def test_ci_actions_and_complete_workflow_remain_least_privilege() -> None:
    workflow = _load_yaml(CI_PATH)
    uses_values = list(_mapping_values_for_key(workflow, "uses"))
    assert uses_values == [
        "actions/checkout@v6",
        TRUFFLEHOG_ACTION,
        "actions/dependency-review-action@v4",
    ]

    for uses in uses_values:
        assert isinstance(uses, str)
        owner = uses.split("/", 1)[0]
        if owner not in {"actions", "github"}:
            assert re.search(r"@[0-9a-f]{40}$", uses)

    scalar_values = [value for value in _walk(workflow) if isinstance(value, str)]
    lowered = [value.lower() for value in scalar_values]
    forbidden_keys = {
        "attestations",
        "deployments",
        "environment",
        "environments",
        "id-token",
        "packages",
        "secrets",
    }
    assert forbidden_keys.isdisjoint(lowered)
    assert "write" not in lowered
    assert not any("secrets." in value or "github.token" in value for value in lowered)
    assert not any("upload-artifact" in value for value in lowered)
    assert not any("--privileged" in value or "docker.sock" in value for value in lowered)
    assert not any(profile in value for profile in FORBIDDEN_PROFILES for value in lowered)


def test_dependabot_ecosystems_schedules_limits_and_groups_are_exact() -> None:
    document = _load_yaml(DEPENDABOT_PATH)
    _dependabot_updates(document)


def test_dependabot_rejects_registries_and_extra_update_keys() -> None:
    top_level = copy.deepcopy(_load_yaml(DEPENDABOT_PATH))
    top_level["registries"] = {}
    with pytest.raises(AssertionError):
        _dependabot_updates(top_level)

    update_registry = copy.deepcopy(_load_yaml(DEPENDABOT_PATH))
    update_registry["updates"][0]["registries"] = ["private"]
    with pytest.raises(AssertionError):
        _dependabot_updates(update_registry)

    extra_update_key = copy.deepcopy(_load_yaml(DEPENDABOT_PATH))
    extra_update_key["updates"][1]["extra-key"] = True
    with pytest.raises(AssertionError):
        _dependabot_updates(extra_update_key)


def test_dependabot_ignores_the_exact_pinned_codex_sdk_and_not_docker() -> None:
    project = _load_yaml(DEPENDABOT_PATH)
    by_ecosystem = _dependabot_updates(project)
    assert "docker" not in by_ecosystem
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]
    assert isinstance(dependencies, list)
    pinned = [
        match.group(1)
        for dependency in dependencies
        if isinstance(dependency, str)
        if (match := re.fullmatch(r"([a-z0-9-]+)==[^;\s]+", dependency)) is not None
    ]
    assert pinned == ["openai-codex"]
    assert by_ecosystem["pip"]["ignore"] == [
        {"dependency-name": pinned[0], "versions": ["*"]}
    ]
