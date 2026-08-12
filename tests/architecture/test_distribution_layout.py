# SPDX-License-Identifier: MIT
# File: tests/architecture/test_distribution_layout.py
# Purpose: Enforce the production-distribution test and ignore boundaries

import ast
import hashlib
import re
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml

REQUIRED_TEST_BOUNDARY_PATHS = {
    ".dockerignore",
    "tests/distribution/test_release_contract.py",
    "tests/distribution/powershell_contract.Tests.ps1",
}

ROOT = Path(__file__).parents[2]
DOCKER_CLI_IMAGE = (
    "docker"
    ":"
    "28.3.3-cli@sha256:"
    "0135662b510037ea581d99c2e5929c5e01185139c0b86986a418bd4da0b98a44"
)
DOCKER_CLI_LITERAL = b"docker" b":"
DOCKERFILE_HEREDOC = re.compile(
    rb"<<(-?)(?:'([^']+)'|\"([^\"]+)\"|([A-Za-z0-9_.-]+))"
)
DOCKERFILE_ESCAPE_CANDIDATE = re.compile(rb"^#\s*escape\b", re.IGNORECASE)
IMAGE_NAME_CHARACTERS = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-"
)
DOCKER_CLI_IMAGE_BYTES = DOCKER_CLI_IMAGE.encode("ascii")
COMPOSE_DOCKER_CLI_SERVICES = (
    "runtime-smoke",
    "runtime-smoke-interrupt",
    "posix-launcher-smoke",
    "posix-launcher-smoke-safety",
)


def _ascii_lines(*lines: str) -> bytes:
    return "\n".join(lines).encode("ascii")


SHELL_DOCKER_CLI_ANCHORS: Mapping[Path, tuple[tuple[bytes, bytes, bytes], ...]] = {
    Path("scripts/posix-launcher-smoke.sh"): (
        (
            b"docker container create \\",
            b"    ' >/dev/null",
            _ascii_lines(
                "docker container create \\",
                '    --name "$init_name" \\',
                '    --label "$owner_label" \\',
                '    --label "$run_label" \\',
                '    --volume "$fixture_volume:/fixture:rw" \\',
                f"    {DOCKER_CLI_IMAGE} \\",
                "    /bin/sh -eu -c '",
                "        mkdir -p /fixture/project /fixture/config /fixture/auth /fixture/state",
                '        printf "immutable-target\\n" > /fixture/project/immutable.txt',
                '        printf "schema-version: 1\\n" > /fixture/config/task-models.yaml',
                '        printf "dummy-auth-must-not-be-printed\\n" > /fixture/auth/auth.json',
                "        chmod 0555 /fixture/project /fixture/config /fixture/auth",
                "        chmod 0444 \\",
                "            /fixture/project/immutable.txt \\",
                "            /fixture/config/task-models.yaml \\",
                "            /fixture/auth/auth.json",
                "        chmod 0555 /fixture/goalrouter",
                "        chmod 0700 /fixture/state",
                "    ' >/dev/null",
            ),
        ),
        (
            b"immutable_before=$(docker run --rm \\",
            b"    /fixture/auth/auth.json)",
            _ascii_lines(
                "immutable_before=$(docker run --rm \\",
                '    --label "$owner_label" \\',
                '    --label "$run_label" \\',
                '    --volume "$fixture_volume:/fixture:ro" \\',
                "    --entrypoint sha256sum \\",
                f"    {DOCKER_CLI_IMAGE} \\",
                "    /fixture/project/immutable.txt \\",
                "    /fixture/auth/auth.json)",
            ),
        ),
        (
            b"version_output=$(docker run --rm \\",
            b"    --json version)",
            _ascii_lines(
                "version_output=$(docker run --rm \\",
                '    --label "$owner_label" \\',
                '    --label "$run_label" \\',
                "    --read-only \\",
                "    --tmpfs /tmp:rw,exec,nosuid,size=64m,mode=1777 \\",
                "    --volume /var/run/docker.sock:/var/run/docker.sock:rw \\",
                '    --volume "$fixture_volume:$fixture_mountpoint:rw" \\',
                "    --env HOME=/tmp/launcher-home \\",
                f"    {DOCKER_CLI_IMAGE} \\",
                '    "$fixture_mountpoint/goalrouter" \\',
                '    --project "$fixture_mountpoint/project" \\',
                '    --config "$fixture_mountpoint/config/task-models.yaml" \\',
                '    --state-dir "$fixture_mountpoint/state" \\',
                '    --codex-home "$fixture_mountpoint/auth" \\',
                '    --image "$image" \\',
                "    --json version)",
            ),
        ),
        (
            b"immutable_after=$(docker run --rm \\",
            b"    /fixture/auth/auth.json)",
            _ascii_lines(
                "immutable_after=$(docker run --rm \\",
                '    --label "$owner_label" \\',
                '    --label "$run_label" \\',
                '    --volume "$fixture_volume:/fixture:ro" \\',
                "    --entrypoint sha256sum \\",
                f"    {DOCKER_CLI_IMAGE} \\",
                "    /fixture/project/immutable.txt \\",
                "    /fixture/auth/auth.json)",
            ),
        ),
    ),
    Path("scripts/posix-launcher-smoke-safety.sh"): (
        (
            b"\ndocker run --rm \\",
            b"    'printf \"collision-sentinel\\n\" > /fixture/sentinel' >/dev/null",
            _ascii_lines(
                "",
                "docker run --rm \\",
                '    --label "$fixture_owner_label" \\',
                '    --label "$fixture_run_label" \\',
                '    --volume "$collision_volume:/fixture:rw" \\',
                "    --entrypoint /bin/sh \\",
                f"    {DOCKER_CLI_IMAGE} -eu -c \\",
                "    'printf \"collision-sentinel\\n\" > /fixture/sentinel' >/dev/null",
            ),
        ),
        (
            b"sentinel=$(docker run --rm \\",
            (
                f"    {DOCKER_CLI_IMAGE} -eu -c 'cat /fixture/sentinel')"
            ).encode("ascii"),
            _ascii_lines(
                "sentinel=$(docker run --rm \\",
                '    --label "$fixture_owner_label" \\',
                '    --label "$fixture_run_label" \\',
                '    --volume "$collision_volume:/fixture:ro" \\',
                "    --entrypoint /bin/sh \\",
                f"    {DOCKER_CLI_IMAGE} -eu -c 'cat /fixture/sentinel')",
            ),
        ),
    ),
    Path("scripts/runtime-image-smoke-interrupt.sh"): (
        (
            b"driver_id=$(docker container create \\",
            b"    /workspace/scripts/runtime-image-smoke.sh)",
            _ascii_lines(
                "driver_id=$(docker container create \\",
                '    --name "$driver_name" \\',
                '    --label "$interrupt_owner_label" \\',
                '    --label "$interrupt_label" \\',
                "    --network none \\",
                "    --read-only \\",
                "    --tmpfs /tmp:rw,exec,nosuid,size=256m,mode=1777 \\",
                "    --env DOCKER_BUILDKIT=1 \\",
                "    --env HOME=/tmp/docker-home \\",
                "    --env RUNTIME_SMOKE_HOLD_AFTER_FIXTURES=1 \\",
                '    --env "RUNTIME_SMOKE_SYNC_LABEL=$sync_label" \\',
                '    --volume "$workspace_source:/workspace:ro" \\',
                "    --volume /var/run/docker.sock:/var/run/docker.sock:rw \\",
                "    --entrypoint /bin/sh \\",
                f"    {DOCKER_CLI_IMAGE} \\",
                "    /workspace/scripts/runtime-image-smoke.sh)",
            ),
        ),
    ),
}
DOCKER_CLI_SOURCE_SHA256: Mapping[Path, str] = {
    Path("Dockerfile"): (
        "f0c28635fcdf03e703b0a5a2158ffeffda61b102eb299b028388f0fa08f53598"
    ),
    Path("compose.yaml"): (
        "36840721cfa8113d712737b40d030cb0ac8fa04d19e98d7e1e117d489354fb35"
    ),
    Path("scripts/posix-launcher-smoke.sh"): (
        "4bec199546a5c10758be487ce612eaeea4d68c0c45b96022c98a5b26c4f8e461"
    ),
    Path("scripts/posix-launcher-smoke-safety.sh"): (
        "5aa53ac388b85ba8d23667a62a2c15a8e19fbfaa555dc77e4dacfb367387eb04"
    ),
    Path("scripts/runtime-image-smoke-interrupt.sh"): (
        "6a7aa1b483b3f87b0b299a246d3d02ea32967870fe00cbd8683cd83059115180"
    ),
    Path("tests/distribution/test_authority_profiles.py"): (
        "d02a26bf0619a33c3699ec5fcc9f525ea4fda4eccda03dad5b50826c982f79d8"
    ),
    Path("tests/distribution/test_posix_launcher.py"): (
        "6407e6ecae9f97633a531b75bd1a463461dbad28b4032582a8356c9954cbdc33"
    ),
    Path("tests/distribution/test_runtime_image.py"): (
        "e6992a427fc75813471421cc4d861762522611862f733f0ec55481806e5b29c7"
    ),
}
TEST_DOCKER_CLI_CONTEXTS: Mapping[Path, Mapping[str, Counter[bytes]]] = {
    Path("tests/distribution/test_authority_profiles.py"): {
        "test_real_launcher_profiles_enforce_mounts_socket_and_numeric_ownership": Counter(
            {
                f'            "{DOCKER_CLI_IMAGE}",'.encode("ascii"): 3,
                f'                "{DOCKER_CLI_IMAGE}",'.encode("ascii"): 2,
            }
        )
    },
    Path("tests/distribution/test_posix_launcher.py"): {
        "test_posix_launcher_smoke_has_a_least_authority_declared_boundary": Counter(
            {
                (
                    f'    assert service["image"] == "{DOCKER_CLI_IMAGE}"'
                    "  # noqa: E501"
                ).encode("ascii"): 1
            }
        )
    },
    Path("tests/distribution/test_runtime_image.py"): {
        "test_runtime_smoke_is_a_declared_least_authority_docker_gate": Counter(
            {
                (
                    f'    assert service["image"] == "{DOCKER_CLI_IMAGE}"'
                    "  # noqa: E501"
                ).encode("ascii"): 1
            }
        ),
        "test_runtime_smoke_cleanup_is_ownership_scoped_and_signal_safe": Counter(
            {
                (
                    f'    assert service["image"] == "{DOCKER_CLI_IMAGE}"'
                    "  # noqa: E501"
                ).encode("ascii"): 1
            }
        ),
    },
}
LOCAL_ARTIFACT_DIRECTORIES = {
    ".git",
    ".goalrouter",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".superpowers",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    "planning",
}
LOCAL_ARTIFACT_FILENAMES = {".coverage"}
LOCAL_ARTIFACT_SUFFIXES = {".pyc", ".pyd", ".pyo"}


def _is_local_artifact(relative: Path) -> bool:
    return (
        bool(LOCAL_ARTIFACT_DIRECTORIES.intersection(relative.parts))
        or relative.name in LOCAL_ARTIFACT_FILENAMES
        or relative.suffix in LOCAL_ARTIFACT_SUFFIXES
        or any(part.endswith(".egg-info") for part in relative.parts)
    )


def _product_file_contents(root: Path = ROOT) -> dict[Path, bytes]:
    contents: dict[Path, bytes] = {}
    for candidate in root.rglob("*"):
        relative = candidate.relative_to(root)
        if _is_local_artifact(relative):
            continue
        assert not candidate.is_symlink(), f"Product source is a symlink: {relative}"
        if not candidate.is_file():
            continue
        contents[relative] = candidate.read_bytes()
    return contents


def _docker_cli_source_contexts(
    contents: Mapping[Path, bytes],
) -> Counter[tuple[Path, bytes]]:
    contexts: Counter[tuple[Path, bytes]] = Counter()
    for path, content in contents.items():
        lines = content.splitlines()
        for line_index, line in enumerate(lines):
            search_start = 0
            while True:
                leaf_start = line.find(DOCKER_CLI_LITERAL, search_start)
                if leaf_start < 0:
                    break
                search_start = leaf_start + len(DOCKER_CLI_LITERAL)
                if (
                    leaf_start > 0
                    and line[leaf_start - 1] in IMAGE_NAME_CHARACTERS
                ):
                    continue
                context = line
                if line_index > 0 and lines[line_index - 1].endswith(b"\\"):
                    context = lines[line_index - 1] + b"\n" + line
                contexts[(path, context)] += 1
    return contexts


def _assert_default_dockerfile_escape(content: bytes) -> None:
    seen_instruction = False
    seen_escape = False
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(b"#"):
            if DOCKERFILE_ESCAPE_CANDIDATE.match(stripped):
                assert not seen_instruction, "Dockerfile escape directive is late"
                assert not seen_escape, "Dockerfile escape directive is duplicated"
                normalized = re.sub(rb"\s+", b"", stripped).lower()
                assert normalized == b"#escape=\\", (
                    "Dockerfile escape directive must use the default backslash",
                    stripped,
                )
                seen_escape = True
            continue
        seen_instruction = True


def _dockerfile_instructions(content: bytes) -> list[tuple[bytes, bytes]]:
    _assert_default_dockerfile_escape(content)
    lines = content.splitlines()
    instructions: list[tuple[bytes, bytes]] = []
    line_index = 0
    while line_index < len(lines):
        first_line = lines[line_index]
        line_index += 1
        if not first_line.strip() or first_line.lstrip().startswith(b"#"):
            continue

        logical_parts: list[bytes] = []
        current_line = first_line
        while True:
            stripped = current_line.rstrip()
            continued = stripped.endswith(b"\\")
            logical_parts.append(stripped[:-1] if continued else current_line)
            if not continued:
                break
            assert line_index < len(lines), "Unterminated Dockerfile continuation"
            current_line = lines[line_index]
            line_index += 1

        logical = b" ".join(part.strip() for part in logical_parts)
        fields = logical.split(maxsplit=1)
        assert len(fields) == 2, ("Invalid Dockerfile instruction", logical)
        keyword, value = fields[0].upper(), fields[1]
        instructions.append((keyword, value))

        heredocs = [
            (match.group(1) == b"-", next(group for group in match.groups()[1:] if group))
            for match in DOCKERFILE_HEREDOC.finditer(logical)
        ]
        for strip_tabs, delimiter in heredocs:
            while line_index < len(lines):
                body_line = lines[line_index]
                line_index += 1
                comparable = body_line.lstrip(b"\t") if strip_tabs else body_line
                if comparable == delimiter:
                    break
            else:
                raise AssertionError(("Unterminated Dockerfile heredoc", delimiter))
    return instructions


def _dockerfile_named_stage_images(content: bytes, stage_name: bytes) -> list[bytes]:
    images: list[bytes] = []
    for keyword, value in _dockerfile_instructions(content):
        if keyword != b"FROM":
            continue
        fields = value.split()
        while fields and fields[0].startswith(b"--"):
            fields.pop(0)
        assert fields, ("Dockerfile FROM has no image", value)
        for field_index, field in enumerate(fields[1:], start=1):
            if field.upper() == b"AS" and field_index + 1 < len(fields):
                if fields[field_index + 1] == stage_name:
                    images.append(fields[0])
                break
    return images


def _assert_compose_docker_cli_anchors(contents: Mapping[Path, bytes]) -> None:
    path = Path("compose.yaml")
    document = yaml.safe_load(contents[path].decode("utf-8"))
    assert isinstance(document, dict)
    services = document.get("services")
    assert isinstance(services, dict)
    actual = {
        service_name: services.get(service_name, {}).get("image")
        for service_name in COMPOSE_DOCKER_CLI_SERVICES
    }
    assert actual == dict.fromkeys(COMPOSE_DOCKER_CLI_SERVICES, DOCKER_CLI_IMAGE)
    assert sum(_docker_cli_source_contexts({path: contents[path]}).values()) == 4


def _assert_dockerfile_docker_cli_anchor(contents: Mapping[Path, bytes]) -> None:
    path = Path("Dockerfile")
    images = _dockerfile_named_stage_images(contents[path], b"posix-installer-smoke")
    assert images == [DOCKER_CLI_IMAGE_BYTES]
    assert sum(_docker_cli_source_contexts({path: contents[path]}).values()) == 1


def _assert_shell_docker_cli_anchors(contents: Mapping[Path, bytes]) -> None:
    for path, anchors in SHELL_DOCKER_CLI_ANCHORS.items():
        content = contents[path]
        for start, end, expected_span in anchors:
            assert content.count(start) == 1, (path, start)
            start_index = content.index(start)
            end_index = content.find(end, start_index + len(start))
            assert end_index >= 0, (path, start, end)
            span = content[start_index : end_index + len(end)]
            assert span.count(DOCKER_CLI_IMAGE_BYTES) == 1, (path, start)
            assert span == expected_span, (path, start)
        assert sum(_docker_cli_source_contexts({path: content}).values()) == len(
            anchors
        )


def _assert_test_docker_cli_anchors(contents: Mapping[Path, bytes]) -> None:
    for path, expected_functions in TEST_DOCKER_CLI_CONTEXTS.items():
        content = contents[path]
        source = content.decode("utf-8")
        lines = content.splitlines()
        tree = ast.parse(source, filename=path.as_posix())
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        }
        for function_name, expected_contexts in expected_functions.items():
            function = functions.get(function_name)
            assert function is not None, (path, function_name)
            actual_contexts: Counter[bytes] = Counter()
            for node in ast.walk(function):
                if isinstance(node, ast.Constant) and node.value == DOCKER_CLI_IMAGE:
                    actual_contexts[lines[node.lineno - 1]] += 1
            assert actual_contexts == expected_contexts, {
                "path": path,
                "function": function_name,
                "missing": expected_contexts - actual_contexts,
                "unexpected": actual_contexts - expected_contexts,
            }
        expected_count = sum(
            sum(function_contexts.values())
            for function_contexts in expected_functions.values()
        )
        assert sum(_docker_cli_source_contexts({path: content}).values()) == expected_count


def _assert_docker_cli_source_integrity(
    contents: Mapping[Path, bytes],
    contexts: Mapping[tuple[Path, bytes], int],
) -> None:
    owner_paths = {path for path, _context in contexts}
    assert owner_paths == DOCKER_CLI_SOURCE_SHA256.keys(), {
        "missing": DOCKER_CLI_SOURCE_SHA256.keys() - owner_paths,
        "unexpected": owner_paths - DOCKER_CLI_SOURCE_SHA256.keys(),
    }
    for path, expected_sha256 in DOCKER_CLI_SOURCE_SHA256.items():
        actual_sha256 = hashlib.sha256(contents[path]).hexdigest()
        assert actual_sha256 == expected_sha256, (path, actual_sha256)


def _assert_docker_cli_image_policy(
    contents: Mapping[Path, bytes],
    *,
    approved_contexts: Counter[tuple[Path, bytes]] | None = None,
) -> None:
    actual = _docker_cli_source_contexts(contents)
    if approved_contexts is not None:
        assert actual == approved_contexts, {
            "missing": approved_contexts - actual,
            "unexpected": actual - approved_contexts,
        }
        return

    _assert_docker_cli_source_integrity(contents, actual)
    assert sum(actual.values()) == 20, actual
    _assert_compose_docker_cli_anchors(contents)
    _assert_dockerfile_docker_cli_anchor(contents)
    _assert_shell_docker_cli_anchors(contents)
    _assert_test_docker_cli_anchors(contents)


def _synthetic_approved_contexts(count: int) -> Counter[tuple[Path, bytes]]:
    reference = DOCKER_CLI_IMAGE.encode("ascii")
    return Counter((Path(f"valid-source-{index}"), reference) for index in range(count))


def test_distribution_test_boundary_exists() -> None:
    missing = sorted(path for path in REQUIRED_TEST_BOUNDARY_PATHS if not Path(path).is_file())

    assert missing == []


def test_local_artifacts_are_ignored() -> None:
    ignored = Path(".gitignore").read_text(encoding="utf-8").splitlines()

    assert {"planning/", ".goalrouter/", ".superpowers/"} <= set(ignored)


def test_every_docker_cli_tool_image_reference_is_digest_pinned() -> None:
    _assert_docker_cli_image_policy(_product_file_contents())


@pytest.mark.parametrize(
    "mutated_reference",
    (
        DOCKER_CLI_IMAGE.split("@", maxsplit=1)[0],
        DOCKER_CLI_IMAGE[:-1] + "5",
        DOCKER_CLI_IMAGE.replace("28.3.3-cli", "28.3.4-cli"),
        DOCKER_CLI_IMAGE.replace("28.3.3-cli", "latest"),
        "evil/" + DOCKER_CLI_IMAGE,
        "alpine:latest",
    ),
    ids=(
        "tag-only",
        "altered-digest",
        "altered-version",
        "latest",
        "repository-prefix",
        "different-image",
    ),
)
def test_docker_cli_image_policy_rejects_mutations(mutated_reference: str) -> None:
    with pytest.raises(AssertionError):
        _assert_docker_cli_image_policy(
            {Path("mutated-source"): mutated_reference.encode("ascii")},
            approved_contexts=Counter(
                {(Path("mutated-source"), DOCKER_CLI_IMAGE.encode("ascii")): 1}
            ),
        )


def test_docker_cli_image_policy_rejects_prefixed_addition_to_valid_inventory() -> None:
    contents = {
        Path(f"valid-source-{index}"): DOCKER_CLI_IMAGE.encode("ascii")
        for index in range(20)
    }
    contents[Path("prefixed-source")] = ("evil/" + DOCKER_CLI_IMAGE).encode("ascii")

    with pytest.raises(AssertionError):
        _assert_docker_cli_image_policy(
            contents, approved_contexts=_synthetic_approved_contexts(20)
        )


@pytest.mark.parametrize(
    "dynamic_prefix",
    (
        "${REGISTRY}/",
        "$REGISTRY/",
        "${REGISTRY:-docker.io/library}/",
        "${REGISTRY:?required}/",
        "$(registry)/",
        "$env:REGISTRY/",
        "%REGISTRY%/",
        "!~^&*()/",
    ),
    ids=(
        "compose-braced",
        "dockerfile-simple",
        "compose-default",
        "shell-required",
        "command-substitution",
        "powershell-environment",
        "percent-expansion",
        "arbitrary-punctuation",
    ),
)
def test_docker_cli_image_policy_rejects_dynamic_prefixed_addition(
    dynamic_prefix: str,
) -> None:
    contents = {
        Path(f"valid-source-{index}"): DOCKER_CLI_IMAGE.encode("ascii")
        for index in range(20)
    }
    contents[Path("dynamic-source")] = (dynamic_prefix + DOCKER_CLI_IMAGE).encode(
        "ascii"
    )

    with pytest.raises(AssertionError):
        _assert_docker_cli_image_policy(
            contents, approved_contexts=_synthetic_approved_contexts(20)
        )


@pytest.mark.parametrize(
    "dynamic_prefix",
    (
        "${REGISTRY}/",
        "$(registry)/",
        "$env:REGISTRY/",
        "%REGISTRY%/",
        "!~^&*()/",
    ),
    ids=("compose", "command", "powershell", "percent", "punctuation"),
)
def test_docker_cli_image_policy_ignores_unrelated_dynamic_image(
    dynamic_prefix: str,
) -> None:
    contents = {
        Path(f"valid-source-{index}"): DOCKER_CLI_IMAGE.encode("ascii")
        for index in range(20)
    }
    contents[Path("unrelated-source")] = (dynamic_prefix + "alpine:latest").encode(
        "ascii"
    )

    _assert_docker_cli_image_policy(
        contents, approved_contexts=_synthetic_approved_contexts(20)
    )


@pytest.mark.parametrize(
    "replacement",
    (
        b'evil/"' + DOCKER_CLI_IMAGE.encode("ascii") + b'"',
        b"evil/\\\n" + DOCKER_CLI_IMAGE.encode("ascii"),
    ),
    ids=("adjacent-quote", "posix-line-continuation"),
)
def test_docker_cli_image_policy_rejects_split_replacement(
    replacement: bytes,
) -> None:
    contents = {
        Path(f"valid-source-{index}"): DOCKER_CLI_IMAGE.encode("ascii")
        for index in range(19)
    }
    contents[Path("replacement-source")] = replacement
    approved = _synthetic_approved_contexts(19)
    approved[(Path("replacement-source"), DOCKER_CLI_IMAGE.encode("ascii"))] = 1

    with pytest.raises(AssertionError):
        _assert_docker_cli_image_policy(contents, approved_contexts=approved)


@pytest.mark.parametrize(
    "addition",
    (
        b'evil/"' + DOCKER_CLI_IMAGE.encode("ascii") + b'"',
        b"evil/\\\n" + DOCKER_CLI_IMAGE.encode("ascii"),
    ),
    ids=("adjacent-quote", "posix-line-continuation"),
)
def test_docker_cli_image_policy_rejects_split_addition(addition: bytes) -> None:
    contents = {
        Path(f"valid-source-{index}"): DOCKER_CLI_IMAGE.encode("ascii")
        for index in range(20)
    }
    contents[Path("addition-source")] = addition

    with pytest.raises(AssertionError):
        _assert_docker_cli_image_policy(
            contents, approved_contexts=_synthetic_approved_contexts(20)
        )


def test_docker_cli_image_policy_rejects_compose_service_relocation() -> None:
    contents = _product_file_contents()
    compose_path = Path("compose.yaml")
    compose = contents[compose_path].decode("utf-8")
    approved_line = f"    image: {DOCKER_CLI_IMAGE}"
    assert compose.count(approved_line) == 4
    compose = compose.replace(approved_line, "    image: ${UNPINNED}")
    compose += "\n".join(
        (
            "",
            "  decoy-one:",
            approved_line,
            "  decoy-two:",
            approved_line,
            "  decoy-three:",
            approved_line,
            "  decoy-four:",
            approved_line,
            "",
        )
    )
    contents[compose_path] = compose.encode("utf-8")

    with pytest.raises(AssertionError):
        _assert_docker_cli_image_policy(contents)


def test_docker_cli_image_policy_rejects_shell_command_relocation() -> None:
    contents = _product_file_contents()
    script_path = Path("scripts/posix-launcher-smoke.sh")
    script = contents[script_path].decode("utf-8")
    assert script.count(DOCKER_CLI_IMAGE) == 4
    script = script.replace(DOCKER_CLI_IMAGE, "${UNPINNED}", 1)
    script += (
        "\nif false; then\n"
        '    --volume "$fixture_volume:/fixture:rw" \\\n'
        f"    {DOCKER_CLI_IMAGE} \\\n"
        "fi\n"
    )
    contents[script_path] = script.encode("utf-8")

    with pytest.raises(AssertionError):
        _assert_docker_cli_image_policy(contents)


def test_docker_cli_image_policy_rejects_dockerfile_heredoc_decoy() -> None:
    contents = _product_file_contents()
    dockerfile_path = Path("Dockerfile")
    dockerfile = contents[dockerfile_path].decode("utf-8")
    approved_from = f"FROM {DOCKER_CLI_IMAGE} AS posix-installer-smoke"
    assert dockerfile.count(approved_from) == 1
    dockerfile = dockerfile.replace(
        approved_from,
        "FROM ${TOOL_REPO}:${TOOL_TAG} AS posix-installer-smoke",
    )
    dockerfile += (
        "\nRUN <<'GOALROUTER_DECOY'\n"
        f"{approved_from}\n"
        "GOALROUTER_DECOY\n"
    )
    contents[dockerfile_path] = dockerfile.encode("utf-8")

    with pytest.raises(AssertionError):
        _assert_docker_cli_image_policy(contents)


def test_docker_cli_image_policy_rejects_backtick_escape_desynchronization() -> None:
    contents = _product_file_contents()
    dockerfile_path = Path("Dockerfile")
    contents[dockerfile_path] = _ascii_lines(
        "# escape=`",
        "ARG DECOY=\\",
        "FROM ${TOOL_REPO}:${TOOL_TAG} AS posix-installer-smoke",
        "RUN printf decoy `",
        f"FROM {DOCKER_CLI_IMAGE} AS posix-installer-smoke",
        "",
    )

    with pytest.raises(AssertionError):
        _dockerfile_instructions(contents[dockerfile_path])


def test_docker_cli_image_policy_accepts_explicit_default_dockerfile_escape() -> None:
    contents = _product_file_contents()
    dockerfile_path = Path("Dockerfile")
    contents[dockerfile_path] = b"# escape=\\\n" + contents[dockerfile_path]

    images = _dockerfile_named_stage_images(
        contents[dockerfile_path], b"posix-installer-smoke"
    )

    assert images == [DOCKER_CLI_IMAGE_BYTES]


@pytest.mark.parametrize(
    ("prefix", "suffix"),
    (
        (b"# escape\n", b""),
        (b"", b"\n# escape=\\\n"),
        (b"# escape=\\\n# escape=\\\n", b""),
    ),
    ids=("malformed", "late", "duplicate"),
)
def test_docker_cli_image_policy_rejects_invalid_dockerfile_escape_directive(
    prefix: bytes,
    suffix: bytes,
) -> None:
    contents = _product_file_contents()
    dockerfile_path = Path("Dockerfile")
    contents[dockerfile_path] = prefix + contents[dockerfile_path] + suffix

    with pytest.raises(AssertionError):
        _dockerfile_instructions(contents[dockerfile_path])


def test_docker_cli_image_policy_rejects_earlier_shell_image_operand() -> None:
    contents = _product_file_contents()
    script_path = Path("scripts/posix-launcher-smoke.sh")
    script = contents[script_path].decode("utf-8")
    anchor = (
        '    --label "$run_label" \\\n'
        '    --volume "$fixture_volume:/fixture:rw" \\\n'
        f"    {DOCKER_CLI_IMAGE} \\\n"
    )
    assert script.count(anchor) == 1
    script = script.replace(
        anchor,
        '    --label "$run_label" \\\n'
        "    ${UNPINNED} \\\n"
        + anchor.split("\n", maxsplit=1)[1],
    )
    contents[script_path] = script.encode("utf-8")

    with pytest.raises(AssertionError):
        _assert_docker_cli_image_policy(contents)


def test_docker_cli_image_policy_rejects_dead_branch_exact_shell_decoy() -> None:
    contents = _product_file_contents()
    script_path = Path("scripts/posix-launcher-smoke.sh")
    script = contents[script_path]
    approved_span = SHELL_DOCKER_CLI_ANCHORS[script_path][0][2]
    assert script.count(approved_span) == 1
    active_span = approved_span.replace(
        b"docker container create \\\n",
        b"docker \\\n    container create \\\n",
        1,
    ).replace(DOCKER_CLI_IMAGE_BYTES, b"${UNPINNED}", 1)
    script = script.replace(approved_span, active_span, 1)
    script += b"\nif false; then\n" + approved_span + b"\nfi\n"
    contents[script_path] = script

    with pytest.raises(AssertionError):
        _assert_docker_cli_image_policy(contents)


def test_docker_cli_image_policy_rejects_unreachable_python_ast_decoy() -> None:
    contents = _product_file_contents()
    test_path = Path("tests/distribution/test_posix_launcher.py")
    source = contents[test_path].decode("utf-8")
    approved_assertion = (
        f'    assert service["image"] == "{DOCKER_CLI_IMAGE}"'
        "  # noqa: E501\n"
    )
    assert source.count(approved_assertion) == 1
    source = source.replace(
        approved_assertion,
        '    assert service["image"] == service["image"]\n'
        "    return\n"
        + approved_assertion,
        1,
    )
    contents[test_path] = source.encode("utf-8")

    with pytest.raises(AssertionError):
        _assert_docker_cli_image_policy(contents)


@pytest.mark.parametrize(
    "source_path",
    tuple(DOCKER_CLI_SOURCE_SHA256),
    ids=lambda path: path.name,
)
def test_docker_cli_image_policy_rejects_one_byte_source_mutation(
    source_path: Path,
) -> None:
    contents = _product_file_contents()
    mutated = bytearray(contents[source_path])
    mutated[0] ^= 1
    contents[source_path] = bytes(mutated)

    with pytest.raises(AssertionError):
        _assert_docker_cli_image_policy(contents)


def test_product_image_scan_rejects_symlink_files(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.write_bytes(DOCKER_CLI_IMAGE.encode("ascii"))
    product = tmp_path / "product"
    product.mkdir()
    (product / "linked-source").symlink_to(external)

    with pytest.raises(AssertionError, match="Product source is a symlink"):
        _product_file_contents(product)


def test_product_image_scan_ignores_symlinks_in_local_artifact_trees(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external"
    external.write_bytes(DOCKER_CLI_IMAGE.encode("ascii"))
    product = tmp_path / "product"
    ignored = product / ".superpowers"
    ignored.mkdir(parents=True)
    (ignored / "linked-report").symlink_to(external)
    (product / ".coverage").symlink_to(external)

    assert _product_file_contents(product) == {}
