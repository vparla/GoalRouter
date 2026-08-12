# SPDX-License-Identifier: MIT
# File: tests/architecture/test_documentation.py
# Purpose: Enforce the complete public documentation contract

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from goalrouter.cli import build_parser

ROOT = Path(__file__).resolve().parents[2]
TOP_LEVEL_DOCUMENTS = (
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CHANGELOG.md",
)
DOC_NAMES = (
    "installation.md",
    "quickstart.md",
    "cli.md",
    "configuration.md",
    "authentication.md",
    "operations.md",
    "security.md",
    "upgrading.md",
    "uninstalling.md",
    "troubleshooting.md",
    "architecture.md",
    "development.md",
    "releasing.md",
    "testing.md",
    "validation-projects.md",
)
CANONICAL_REPOSITORY = "vparla/GoalRouter"
CANONICAL_IMAGE = "ghcr.io/vparla/goalrouter"
CURRENT_VERSION = "1.0.4"
CONTRIBUTOR_DOCUMENTS = (
    "CONTRIBUTING.md",
    "docs/development.md",
    "docs/testing.md",
    "docs/releasing.md",
)
SECURITY_REMEDIATION_DOCUMENTATION = (
    (
        "descriptor-rooted-instructions",
        "docs/architecture.md",
        "Repository instruction discovery is descriptor-rooted beneath the resolved "
        "project and uses no-follow semantics for every path component and final file.",
    ),
    (
        "unsafe-instructions-before-sdk",
        "docs/security.md",
        "Plan and run-creation workflows reject instruction symbolic links and "
        "non-regular files before those workflows make a Codex SDK or model call.",
    ),
    (
        "instruction-ordering",
        "docs/operations.md",
        "Planning inspects repository metadata and applicable instruction files before "
        "it validates model inventory.",
    ),
    (
        "project-contention-exit",
        "docs/cli.md",
        "Project writer contention fails immediately with exit `14`.",
    ),
    (
        "run-contention-exit",
        "docs/cli.md",
        "Run contention fails immediately with exit `15`.",
    ),
    (
        "writer-specific-contention",
        "docs/security.md",
        "Project contention does not dispatch the contending writer or create a failed "
        "work result; readers completed before that point remain recorded.",
    ),
    (
        "no-target-lockfile",
        "docs/operations.md",
        "The project directory lease creates no lockfile or sentinel in the target project.",
    ),
    (
        "retry-after-owner-exits",
        "docs/troubleshooting.md",
        "Retry only after the process or container holding the lease exits; kernel "
        "ownership is then released even after a crash.",
    ),
    (
        "status-is-unleased-snapshot",
        "docs/cli.md",
        "`status` is a non-mutating snapshot and does not acquire the run lease.",
    ),
    (
        "busy-state-normalization",
        "docs/operations.md",
        "When an approved writer reaches project contention from `awaiting-approval` "
        "or transient `running`, its checkpoint is normalized to `planned`; `failed` "
        "and `blocked` states are not rewritten.",
    ),
    (
        "docker-cli-digest-policy",
        "docs/development.md",
        "Every Docker CLI tool-image consumer is pinned by exact version and OCI index digest.",
    ),
    (
        "wsl-direct-evidence",
        "docs/testing.md",
        "The same-bind writer contention probe is directly verified on Docker Desktop "
        "through WSL.",
    ),
    (
        "native-proof-deferred",
        "docs/testing.md",
        "Clean-host Linux Docker Engine and macOS Docker Desktop proof remains a "
        "release gate; it is not inferred from the WSL result.",
    ),
    (
        "git-subprocess-boundary",
        "docs/architecture.md",
        "Repository Git inspection begins only after descriptor-rooted instruction "
        "preflight succeeds. It executes absolute `/usr/bin/git` with an explicit "
        "minimal environment, disables Git filesystem monitors and Git hooks at command scope, "
        "and pins the validated worktree, Git directory, and index identity around evidence "
        "reads.",
    ),
    (
        "git-configuration-execution",
        "docs/security.md",
        "Repository-controlled local or included Git configuration cannot enable "
        "Git filesystem-monitor or Git-hook execution during inspection. Inherited Git, pager, "
        "prompt, loader, and configuration environment variables are not propagated.",
    ),
    (
        "git-exact-safe-directory",
        "docs/security.md",
        "GoalRouter adds one command-scoped safe-directory exception for the exact lexical "
        "worktree candidate so read-only Docker mounts remain inspectable; it never trusts a "
        "wildcard or a different discovered root.",
    ),
    (
        "git-dirty-read-boundary",
        "docs/operations.md",
        "Dirty paths are composed from strict NUL-delimited index, HEAD-tree, and "
        "untracked-name metadata plus GoalRouter-owned raw blob hashing through already-open "
        "no-follow descriptors.",
    ),
    (
        "linked-worktree-boundary",
        "docs/architecture.md",
        "Linked worktrees are supported after their discovered worktree and Git-directory "
        "paths validate. Unsafe lexical `.git` entry types fail before Git starts; "
        "malformed gitfiles or discovery evidence fail during the single hardened "
        "discovery command and before branch, dirty-evidence, planner, or model work.",
    ),
    (
        "git-inspection-recovery",
        "docs/troubleshooting.md",
        "Exit `5` during Git inspection can indicate an unsafe `.git` entry, malformed "
        "discovered paths, output limit, timeout, or source-control ownership refusal. "
        "Repair repository metadata with trusted Git tooling; do not add "
        "`safe.directory=*` or delete another worktree's administrative files.",
    ),
    (
        "git-security-fixtures",
        "docs/testing.md",
        "Real Git security fixtures prove local, included, global, system, and "
        "environment-injected filesystem monitors cannot execute; inspection leaves the "
        "index bytes and metadata unchanged and creates no `index.lock`.",
    ),
)


def _public_docs() -> tuple[Path, ...]:
    return (
        *(ROOT / name for name in TOP_LEVEL_DOCUMENTS),
        *(ROOT / "docs" / name for name in DOC_NAMES),
    )


def _public_doc_texts() -> dict[str, str]:
    return {
        str(path.relative_to(ROOT)): path.read_text(encoding="utf-8")
        for path in _public_docs()
    }


def _normalize_documentation(content: str) -> str:
    return " ".join(content.split())


def _assert_security_remediation_documentation(
    documents: Mapping[str, str],
) -> None:
    for claim_id, name, claim in SECURITY_REMEDIATION_DOCUMENTATION:
        assert claim in _normalize_documentation(documents[name]), claim_id


def _complete_security_remediation_documentation() -> dict[str, str]:
    documents = {
        name: "" for _claim_id, name, _claim in SECURITY_REMEDIATION_DOCUMENTATION
    }
    for _claim_id, name, claim in SECURITY_REMEDIATION_DOCUMENTATION:
        documents[name] = f"{documents[name]}\n{claim}"
    return documents


def _assert_relative_links_resolve(documents: Mapping[str, str]) -> None:
    link = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")
    for name, content in documents.items():
        for target in link.findall(content):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target, flags=re.IGNORECASE):
                continue
            relative = target.split("#", 1)[0]
            if not relative:
                continue
            destination = (ROOT / name).parent / relative
            assert destination.is_file(), f"broken relative link in {name}: {target}"


def _section(content: str, heading: str) -> str:
    match = re.search(
        rf"(?ms)^## {re.escape(heading)}\s*$\n(.*?)(?=^## |\Z)",
        content,
    )
    assert match is not None, f"missing section: {heading}"
    return match.group(1)


def _launcher_help(script: str) -> str:
    match = re.search(r"Usage: goalrouter .*?\n(?:EOF|'@)", script, flags=re.DOTALL)
    assert match is not None
    return match.group(0)


def _table_commands(section: str) -> set[str]:
    return set(re.findall(r"(?m)^\| `([a-z]+)(?: [^`]*)?` \|", section))


def _parser_commands() -> set[str]:
    action = next(action for action in build_parser()._actions if action.dest == "command")
    return set(action.choices or {})


def _schema_property_names(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        names = set(value.get("properties", {}))
        for child in value.values():
            names.update(_schema_property_names(child))
        return names
    if isinstance(value, list):
        names: set[str] = set()
        for child in value:
            names.update(_schema_property_names(child))
        return names
    return set()


def _assert_wsl_transport_only(documents: Mapping[str, str]) -> None:
    prose = " ".join(
        re.sub(r"```.*?```", "", content, flags=re.DOTALL)
        for content in documents.values()
    )
    sentences = re.split(r"(?<=[.!?])\s+", " ".join(prose.split()))
    wsl_sentences = [sentence for sentence in sentences if re.search(r"\bWSL\b", sentence)]
    assert any(
        "routing" in sentence.lower()
        and ("only" in sentence.lower() or "limited" in sentence.lower())
        for sentence in wsl_sentences
    )

    authority = re.compile(
        r"\bWSL\b(?:\s+\w+){0,4}\s+"
        r"(?:may|can|is\s+(?:allowed|permitted)\s+to)\s+[^.!?]*"
        r"\b(?:inspect|edit|read|write|modify|run|execute|invoke|install|test|build|"
        r"lint|type-check|package|repository tools?)\b",
        flags=re.IGNORECASE,
    )
    explicit_denial = re.compile(
        r"(?:\bneither\b[^.!?]*\bnor\s+WSL\b|"
        r"\bWSL\b[^.!?]{0,40}\b(?:does|must|may|can)\s+not\b)",
        flags=re.IGNORECASE,
    )
    for sentence in wsl_sentences:
        assert authority.search(sentence) is None or explicit_denial.search(sentence) is not None


def _assert_non_root_claims_are_qualified(documents: Mapping[str, str]) -> None:
    runtime_identity_claim = re.compile(
        r"(?:\b(?:runtime|container|image)\b[^.!?]{0,160}"
        r"\b(?:non-root|unprivileged|rootless)\b|"
        r"\b(?:non-root|unprivileged|rootless)\b[^.!?]{0,160}"
        r"\b(?:runtime|container|image|account|user)\b)",
        flags=re.IGNORECASE,
    )
    for name, content in documents.items():
        for paragraph in re.split(r"\n\s*\n", content):
            normalized = " ".join(paragraph.split())
            if runtime_identity_claim.search(normalized) is None:
                continue
            assert "posix" in normalized.lower(), name
            assert re.search(
                r"\binvok\w*\b[^.!?]{0,80}\bUID/GID\b",
                normalized,
                flags=re.IGNORECASE,
            ), name
            assert re.search(
                r"\broot invocation\b[^.!?]{0,120}\b(?:container|runtime)\b"
                r"[^.!?]{0,80}\broot\b",
                normalized,
                flags=re.IGNORECASE,
            ), name


def test_complete_public_documentation_set_exists() -> None:
    assert all(path.is_file() for path in _public_docs())
    assert not (ROOT / "docs" / "safety.md").exists()


def test_public_documentation_relative_links_resolve() -> None:
    _assert_relative_links_resolve(_public_doc_texts())


def test_relative_link_audit_rejects_missing_target() -> None:
    documents = _public_doc_texts()
    documents["README.md"] += "\n[Missing](docs/does-not-exist.md)\n"
    with pytest.raises(AssertionError, match="broken relative link"):
        _assert_relative_links_resolve(documents)


def test_public_docs_are_self_contained_and_follow_runtime_boundaries() -> None:
    bare_host_tool = re.compile(r"(?m)^(?:python|pip|pytest|ruff|mypy)\s")
    api_key_requirement = re.compile(
        r"GoalRouter (?:requires|needs) (?:an? )?API[ -]key",
        flags=re.IGNORECASE,
    )
    for path in _public_docs():
        content = path.read_text(encoding="utf-8")
        lowered = content.lower()
        assert "planning/" not in lowered
        assert ".superpowers/" not in lowered
        for match in re.finditer(r"\bhooks?\b", content, flags=re.IGNORECASE):
            line_start = content.rfind("\n", 0, match.start()) + 1
            line_end = content.find("\n", match.end())
            if line_end == -1:
                line_end = len(content)
            assert "git" in content[line_start:line_end].casefold(), (
                f"non-Git hook claim in {path}"
            )
        assert bare_host_tool.search(content) is None
        assert api_key_requirement.search(content) is None


def test_normal_user_docs_use_the_installed_launcher() -> None:
    normal_user_docs = (
        "README.md",
        "docs/installation.md",
        "docs/quickstart.md",
        "docs/cli.md",
        "docs/configuration.md",
        "docs/authentication.md",
        "docs/operations.md",
        "docs/security.md",
        "docs/upgrading.md",
        "docs/uninstalling.md",
        "docs/troubleshooting.md",
    )
    for name in normal_user_docs:
        content = (ROOT / name).read_text(encoding="utf-8")
        assert "docker compose" not in content.lower()


def test_native_launcher_options_and_maintenance_commands_match_cli_docs() -> None:
    posix_help = _launcher_help((ROOT / "scripts" / "goalrouter").read_text(encoding="utf-8"))
    windows_help = _launcher_help(
        (ROOT / "scripts" / "goalrouter.ps1").read_text(encoding="utf-8")
    )
    assert posix_help.replace("EOF", "").strip() == windows_help.replace("'@", "").strip()

    cli = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    launcher_section = _section(cli, "Native launcher")
    documented_options = set(re.findall(r"(?m)^\| `(--[a-z-]+)` \|", launcher_section))
    implemented_options = set(re.findall(r"^  (--[a-z-]+)", posix_help, flags=re.MULTILINE))
    assert documented_options == implemented_options

    documented_maintenance = _table_commands(_section(cli, "Maintenance commands"))
    maintenance_block = posix_help.split("Maintenance commands:\n", 1)[1]
    implemented_maintenance = set(re.findall(r"^  ([a-z]+)$", maintenance_block, re.MULTILINE))
    assert documented_maintenance == implemented_maintenance


def test_python_application_commands_and_options_match_cli_docs() -> None:
    cli = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    application_section = _section(cli, "Application commands")
    assert _table_commands(application_section) == _parser_commands()

    parser_options = {
        option
        for action in build_parser()._actions
        for option in action.option_strings
        if option.startswith("--")
    }
    command_action = next(
        action for action in build_parser()._actions if action.dest == "command"
    )
    for command_parser in (command_action.choices or {}).values():
        parser_options.update(
            option
            for action in command_parser._actions
            for option in action.option_strings
            if option.startswith("--")
        )
        for action in command_parser._actions:
            choices = getattr(action, "choices", None)
            if isinstance(choices, Mapping):
                for nested_parser in choices.values():
                    parser_options.update(
                        option
                        for nested_action in nested_parser._actions
                        for option in nested_action.option_strings
                        if option.startswith("--")
                    )
    for option in parser_options:
        assert option in application_section


def test_schema_properties_match_configuration_reference() -> None:
    schema_names: set[str] = set()
    for name in ("task-models.schema.json", "planner-output.schema.json"):
        schema = json.loads((ROOT / "config" / name).read_text(encoding="utf-8"))
        schema_names.update(_schema_property_names(schema))

    configuration = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    documented_names = set(re.findall(r"(?m)^\| `([a-z-]+)` \|", configuration))
    assert documented_names == schema_names


def test_canonical_release_identity_and_version_are_published() -> None:
    for name in ("README.md", "docs/releasing.md"):
        content = (ROOT / name).read_text(encoding="utf-8")
        assert CANONICAL_REPOSITORY in content
        assert CANONICAL_IMAGE in content
        assert CURRENT_VERSION in content


def test_readme_has_install_quickstart_access_authentication_and_complete_docs_map() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for phrase in (
        "Inspect, download, verify, and install",
        "Windows",
        "POSIX",
        "Five-minute read-only quickstart",
        "existing-session",
        "does not require an API key",
        "readonly",
        "write",
        "docker",
    ):
        assert phrase in readme
    for name in DOC_NAMES:
        assert f"docs/{name}" in readme


def test_security_remediation_behavior_is_documented() -> None:
    _assert_security_remediation_documentation(_public_doc_texts())


@pytest.mark.parametrize(
    ("claim_id", "name", "claim"),
    SECURITY_REMEDIATION_DOCUMENTATION,
    ids=[claim_id for claim_id, _name, _claim in SECURITY_REMEDIATION_DOCUMENTATION],
)
def test_security_remediation_contract_rejects_removed_claim(
    claim_id: str,
    name: str,
    claim: str,
) -> None:
    del claim_id
    documents = _complete_security_remediation_documentation()
    assert claim in documents[name]
    documents[name] = documents[name].replace(claim, "", 1)
    with pytest.raises(AssertionError):
        _assert_security_remediation_documentation(documents)


def test_authentication_security_lifecycle_and_release_contracts_are_documented() -> None:
    authentication = (ROOT / "docs" / "authentication.md").read_text(encoding="utf-8")
    assert "https://developers.openai.com/codex/auth/" in authentication
    assert "codex login" in authentication
    assert "environment only" in authentication
    assert "never silently falls back" in authentication
    assert "goalrouter doctor" in authentication

    upgrading = (ROOT / "docs" / "upgrading.md").read_text(encoding="utf-8")
    uninstalling = (ROOT / "docs" / "uninstalling.md").read_text(encoding="utf-8")
    assert "goalrouter update" in upgrading
    assert "preserves" in upgrading
    assert "goalrouter uninstall --purge --yes" in uninstalling
    assert "goalrouter uninstall -Purge -Yes" in uninstalling
    assert "goalrouter uninstall -Yes" in uninstalling
    assert "Windows interactive" not in uninstalling
    assert "preserves" in uninstalling

    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "GitHub Security Advisories" in security
    assert "1.x" in security

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [1.0.4] - 2026-08-12" in changelog
    assert "## [1.0.3] - 2026-08-12" in changelog
    assert "## [1.0.2] - 2026-08-12" in changelog
    assert "## [1.0.1] - 2026-08-12" in changelog
    assert "## [1.0.0] - 2026-08-04" in changelog

    releasing = (ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")
    for phrase in (
        "successful green `main`",
        "annotated tag",
        "protected `release` environment",
        "attestation",
        "anonymous pull",
        "public",
        "rollback",
        "moving tag",
    ):
        assert phrase in releasing


def test_documented_windows_validation_limitation_is_explicit() -> None:
    testing = (ROOT / "docs" / "testing.md").read_text(encoding="utf-8")
    assert "Windows PowerShell 5.1" in testing
    assert "structural and contract coverage" in testing
    assert "native live run" in testing


def test_public_docs_do_not_claim_deferred_native_or_publication_gates_completed() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    installation = (ROOT / "docs" / "installation.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert "verified Windows and POSIX installation" not in readme
    assert "Purpose: Verified native GoalRouter installation" not in installation
    assert "multi-architecture GHCR publication," not in changelog
    assert "multi-architecture GHCR publication workflow" in changelog


def test_platform_specific_installed_image_authority_is_documented() -> None:
    cli = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    architecture = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    for content in (cli, architecture):
        normalized = " ".join(content.split())
        assert "POSIX installed mode rejects a foreign `--image` override" in normalized
        assert "Windows ordinary installed runs" in normalized
        assert "syntactically valid explicit `--image`" in normalized
        assert "does not change trusted installation metadata" in normalized
        assert "maintenance, update, and doctor" in normalized
        assert "trusted digest only" in normalized
        assert "reduced-trust" in normalized


def test_cli_version_does_not_claim_image_creation_metadata() -> None:
    cli = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    assert "creation metadata" not in cli


def test_architecture_limits_completion_review_and_planner_authority() -> None:
    architecture = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    normalized = " ".join(architecture.split())
    assert "Completion review applies only to objective runs" in normalized
    assert "planner-requested access" in normalized
    assert "writer serialization" in normalized
    assert "cannot grant launcher mounts" in normalized
    assert "cannot grant sandbox authority" in normalized


def test_instruction_inspection_precedes_model_inventory_in_public_flow() -> None:
    architecture = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    request_flow = _section(architecture, "Request and routing flow")
    assert request_flow.index("Inspect repository instructions") < request_flow.index(
        "Validate all configured models"
    )

    operations = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
    planning = _normalize_documentation(_section(operations, "Plan an objective"))
    assert (
        "Planning inspects repository metadata and applicable instruction files before "
        "it validates model inventory."
    ) in planning


def test_posix_identity_warning_qualifies_non_root_claim() -> None:
    for name in ("docs/architecture.md", "docs/security.md"):
        content = (ROOT / name).read_text(encoding="utf-8")
        normalized = " ".join(content.split())
        assert "invoking UID/GID" in normalized
        assert "root invocation runs the container as root" in normalized
        assert "Do not install or invoke GoalRouter as root" in normalized


def test_configuration_names_both_shipped_schemas() -> None:
    configuration = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    assert "task-models.schema.json" in configuration
    assert "planner-output.schema.json" in configuration


def test_release_version_synchronization_names_every_enforced_source_surface() -> None:
    releasing = (ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")
    version_section = _section(releasing, "Version synchronization")
    for surface in (
        "pyproject.toml",
        "src/goalrouter/__init__.py",
        "Dockerfile",
        "compose.live.yaml",
        "scripts/install.ps1",
        ".github/workflows/publish.yml",
    ):
        assert surface in version_section


def test_development_limits_wsl_to_directory_and_docker_routing() -> None:
    documents = {
        name: (ROOT / name).read_text(encoding="utf-8")
        for name in CONTRIBUTOR_DOCUMENTS
    }
    _assert_wsl_transport_only(documents)


@pytest.mark.parametrize(
    "contradiction",
    (
        "WSL may inspect repository files.",
        "WSL can edit files directly.",
        "WSL is permitted to run repository tools.",
    ),
)
def test_wsl_transport_invariant_rejects_contradictory_authority(
    contradiction: str,
) -> None:
    documents = {
        name: (ROOT / name).read_text(encoding="utf-8")
        for name in CONTRIBUTOR_DOCUMENTS
    }
    documents["docs/development.md"] += f"\n\n{contradiction}\n"
    with pytest.raises(AssertionError):
        _assert_wsl_transport_only(documents)


def test_no_public_doc_claims_an_unqualified_non_root_runtime() -> None:
    _assert_non_root_claims_are_qualified(_public_doc_texts())


@pytest.mark.parametrize(
    "claim",
    (
        "The runtime always executes under a non-root account.",
        "The container always uses an unprivileged account.",
        "The image guarantees rootless execution.",
    ),
)
def test_non_root_invariant_rejects_equivalent_unqualified_claims(claim: str) -> None:
    documents = _public_doc_texts()
    documents["README.md"] += f"\n\n{claim}\n"
    with pytest.raises(AssertionError):
        _assert_non_root_claims_are_qualified(documents)


def test_non_root_invariant_requires_qualification_local_to_the_claim() -> None:
    documents = _public_doc_texts()
    security = documents["docs/security.md"]
    documents["docs/security.md"] = security.replace(
        "The runtime image declares a non-root UID/GID, but the POSIX launcher "
        "maps the invoking\n"
        "UID/GID into the container; root invocation runs the container as root. "
        "Do not install or\n"
        "invoke GoalRouter as root.",
        "The runtime image declares a non-root UID/GID.",
    )
    assert documents["docs/security.md"] != security
    with pytest.raises(AssertionError):
        _assert_non_root_claims_are_qualified(documents)
