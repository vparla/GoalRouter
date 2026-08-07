# SPDX-License-Identifier: MIT
# File: tests/unit/test_repository.py
# Purpose: Verify asynchronous read-only repository evidence discovery

import asyncio
import errno
import os
import shlex
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import pytest

import goalrouter.repository as repository_module
from goalrouter.errors import RepositoryError
from goalrouter.repository import (
    CommandResult,
    CommandRunnerProtocol,
    LocalCommandRunner,
    LocalRepositoryInspector,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/repository"


class FakeCommandRunner:
    def __init__(
        self,
        *,
        root: Path,
        fail: bool = False,
        error_detail: str = "not a git worktree",
    ) -> None:
        self.root = root
        self.fail = fail
        self.error_detail = error_detail
        self.calls: list[tuple[str, ...]] = []

    async def run_read_only(self, argv: Sequence[str], *, cwd: Path) -> CommandResult:
        call = tuple(argv)
        self.calls.append(call)
        if self.fail:
            return CommandResult(call, 128, "", self.error_detail)
        if "--absolute-git-dir" in call:
            return CommandResult(call, 0, f"{self.root}\n{self.root / '.git'}\n", "")
        if call[-1] == "--show-current":
            return CommandResult(call, 0, "feature/evidence\n", "")
        if "--stage" in call:
            object_id = "a" * 40
            return CommandResult(
                call, 0, f"H 100644 {object_id} 0\tsrc/existing.py\0", ""
            )
        if "ls-tree" in call:
            object_id = "a" * 40
            return CommandResult(
                call, 0, f"100644 blob {object_id}\tsrc/existing.py\0", ""
            )
        if "--others" in call:
            return CommandResult(call, 0, "new.txt\0", "")
        raise AssertionError(f"Unexpected command: {call}")


class MutationProtocol(Protocol):
    def __call__(self) -> None: ...


class MutatingCommandRunner(FakeCommandRunner):
    def __init__(self, *, root: Path, mutation: MutationProtocol) -> None:
        super().__init__(root=root)
        self._mutation = mutation
        self._mutated = False

    async def run_read_only(self, argv: Sequence[str], *, cwd: Path) -> CommandResult:
        if not self._mutated:
            self._mutated = True
            self._mutation()
        return await super().run_read_only(argv, cwd=cwd)


def _accepts_protocol(runner: CommandRunnerProtocol) -> CommandRunnerProtocol:
    return runner


def _run_fixture_git(*arguments: str, cwd: Path) -> None:
    subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        check=True,
        env={
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": "/nonexistent",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _commit_fixture(project: Path, relative: str = "tracked.txt") -> None:
    (project / relative).write_text("tracked\n", encoding="utf-8")
    _run_fixture_git("add", relative, cwd=project)
    _run_fixture_git(
        "-c",
        "user.name=GoalRouter Test",
        "-c",
        "user.email=goalrouter@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "fixture",
        cwd=project,
    )


def _stream(data: bytes = b"", *, eof: bool = True) -> asyncio.StreamReader:
    stream = asyncio.StreamReader()
    if data:
        stream.feed_data(data)
    if eof:
        stream.feed_eof()
    return stream


class _SignalingStreamReader(asyncio.StreamReader):
    def __init__(self, started: asyncio.Event) -> None:
        super().__init__()
        self._started = started

    async def read(self, n: int = -1) -> bytes:
        self._started.set()
        return await super().read(n)


def _blocking_stream(started: asyncio.Event) -> asyncio.StreamReader:
    return _SignalingStreamReader(started)


@pytest.mark.asyncio
async def test_blob_hasher_kills_and_reaps_a_blocked_inherited_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_descriptor, write_descriptor = os.pipe()
    killed_pids: list[int] = []
    real_kill_process_group = repository_module._kill_process_group

    def record_kill(pid: int) -> None:
        killed_pids.append(pid)
        real_kill_process_group(pid)

    monkeypatch.setattr(repository_module, "_kill_process_group", record_kill)
    try:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.1):
                await repository_module.LocalBlobHasher().hash_descriptors(
                    (
                        repository_module._BlobHashRequest(
                            descriptor=read_descriptor,
                            size=1,
                        ),
                    ),
                    algorithm="sha1",
                )
    finally:
        os.close(read_descriptor)
        os.close(write_descriptor)

    assert len(killed_pids) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(killed_pids[0], 0)


@pytest.mark.asyncio
async def test_blob_hasher_uses_one_stdin_disabled_helper_for_a_bounded_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    descriptors = (os.open(first, os.O_RDONLY), os.open(second, os.O_RDONLY))
    expected = tuple(
        repository_module._hash_git_blob("sha1", content)
        for content in (b"first", b"second")
    )
    captured: dict[str, object] = {}

    class CompletedProcess:
        returncode: int | None = 0
        pid = 12345
        stdout = _stream("".join(f"{digest}\n" for digest in expected).encode())
        stderr = _stream()

        async def wait(self) -> int:
            return 0

    async def create_subprocess(*argv: str, **kwargs: object) -> CompletedProcess:
        captured["argv"] = argv
        captured.update(kwargs)
        return CompletedProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)
    try:
        digests = await repository_module.LocalBlobHasher().hash_descriptors(
            tuple(
                repository_module._BlobHashRequest(descriptor=descriptor, size=5 + index)
                for index, descriptor in enumerate(descriptors)
            ),
            algorithm="sha1",
        )
    finally:
        for descriptor in descriptors:
            os.close(descriptor)

    assert digests == expected
    assert captured["pass_fds"] == descriptors
    assert captured["stdin"] is asyncio.subprocess.DEVNULL
    assert captured["start_new_session"] is True


def test_repository_inspector_requires_an_explicit_timeout() -> None:
    with pytest.raises(TypeError, match="timeout_seconds"):
        LocalRepositoryInspector()  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_rejects_relative_nonexistent_and_non_directory_paths(tmp_path: Path) -> None:
    inspector = LocalRepositoryInspector(
        FakeCommandRunner(root=tmp_path), timeout_seconds=10
    )
    relative = Path("relative/project")
    missing = tmp_path / "missing"
    file_path = tmp_path / "file.txt"
    file_path.write_text("not a directory", encoding="utf-8")

    for invalid in (relative, missing, file_path):
        with pytest.raises(RepositoryError):
            await inspector.inspect(invalid)


@pytest.mark.asyncio
async def test_discovers_instructions_git_dirty_state_languages_and_docker(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project)
    (project / ".git").mkdir()
    target = project / "nested"
    (project / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (project / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    source = project / "src"
    source.mkdir()
    (source / "one.py").write_text("value = 1\n", encoding="utf-8")
    (source / "two.py").write_text("value = 2\n", encoding="utf-8")
    (source / "native.cpp").write_text("int main() {}\n", encoding="utf-8")
    ignored_python = project / ".venv/lib"
    ignored_python.mkdir(parents=True)
    (ignored_python / "dependency.py").write_text("ignored = True\n", encoding="utf-8")
    ignored_rust = project / "target/debug"
    ignored_rust.mkdir(parents=True)
    (ignored_rust / "generated.rs").write_text("// ignored\n", encoding="utf-8")
    runner = FakeCommandRunner(root=project)

    context = await LocalRepositoryInspector(
        _accepts_protocol(runner), timeout_seconds=10
    ).inspect(target)

    assert context.project_path == target.resolve()
    assert context.is_git_worktree is True
    assert context.branch == "feature/evidence"
    assert context.dirty_paths == (Path("new.txt"), Path("src/existing.py"))
    assert tuple(item.path for item in context.instruction_files) == (
        project / "AGENTS.md",
        project / "SKILLS.md",
        target / "AGENTS.md",
    )
    assert context.instruction_files[0].content.startswith("<!-- SPDX-License-Identifier")
    assert context.language_counts == (("c++", 1), ("python", 2))
    assert context.docker_files == (project / "Dockerfile", project / "compose.yaml")
    assert context.command_errors == ()
    assert all(call[0] == "/usr/bin/git" for call in runner.calls)


@pytest.mark.asyncio
async def test_repository_fsmonitor_cannot_execute_before_instruction_rejection(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _run_fixture_git("init", "--quiet", cwd=project)
    sentinel = tmp_path / "fsmonitor-executed"
    fsmonitor = tmp_path / "malicious-fsmonitor"
    fsmonitor.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' invoked > {shlex.quote(str(sentinel))}\n",
        encoding="utf-8",
    )
    fsmonitor.chmod(0o700)
    _run_fixture_git("config", "core.fsmonitor", str(fsmonitor), cwd=project)
    credential = tmp_path / "DUMMY-CREDENTIAL"
    credential.write_text("DUMMY-NON-SECRET", encoding="utf-8")
    (project / "AGENTS.md").symlink_to(credential)

    with pytest.raises(RepositoryError, match=r"(?i)unsafe repository instruction"):
        await LocalRepositoryInspector(timeout_seconds=10).inspect(project)

    assert not sentinel.exists(), "repository-controlled core.fsmonitor executed"


def _configure_filter_attack(
    project: Path,
    tmp_path: Path,
    *,
    driver_kind: str,
    attributes_source: str,
) -> tuple[Path, Path]:
    sentinel = tmp_path / f"{attributes_source}-{driver_kind}-filter-executed"
    pid_file = tmp_path / f"{attributes_source}-{driver_kind}-filter.pid"
    executable = tmp_path / f"{attributes_source}-{driver_kind}-filter"
    if driver_kind == "clean":
        executable.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' invoked > {shlex.quote(str(sentinel))}\n"
            "cat\n",
            encoding="utf-8",
        )
        _run_fixture_git(
            "config", "filter.goalrouter-review.clean", str(executable), cwd=project
        )
    else:
        executable.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' invoked > {shlex.quote(str(sentinel))}\n"
            f"printf '%s\\n' \"$$\" > {shlex.quote(str(pid_file))}\n"
            "exec sleep 60\n",
            encoding="utf-8",
        )
        _run_fixture_git(
            "config", "filter.goalrouter-review.process", str(executable), cwd=project
        )
        _run_fixture_git(
            "config", "filter.goalrouter-review.required", "true", cwd=project
        )
    executable.chmod(0o700)
    attributes = (
        project / ".gitattributes"
        if attributes_source == "worktree"
        else project / ".git/info/attributes"
    )
    attributes.write_text(
        "tracked.txt filter=goalrouter-review\n", encoding="utf-8"
    )
    (project / "tracked.txt").write_text("changed\n", encoding="utf-8")
    return sentinel, pid_file


def _write_filter_attributes(
    project: Path, *, attributes_source: str
) -> None:
    attributes = (
        project / ".gitattributes"
        if attributes_source == "worktree"
        else project / ".git/info/attributes"
    )
    attributes.write_text(
        "tracked.txt filter=goalrouter-review\n", encoding="utf-8"
    )
    if attributes_source == "worktree":
        _run_fixture_git("add", ".gitattributes", cwd=project)


@pytest.mark.asyncio
@pytest.mark.parametrize("attributes_source", ("worktree", "git-directory"))
async def test_repository_clean_filters_are_never_executed(
    tmp_path: Path,
    attributes_source: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _run_fixture_git("init", "--quiet", cwd=project)
    _write_filter_attributes(project, attributes_source=attributes_source)
    _commit_fixture(project)
    sentinel, pid_file = _configure_filter_attack(
        project,
        tmp_path,
        driver_kind="clean",
        attributes_source=attributes_source,
    )

    context = await LocalRepositoryInspector(timeout_seconds=1).inspect(project)

    assert context.dirty_paths == (Path("tracked.txt"),)
    assert not sentinel.exists(), "repository-controlled clean filter executed"
    assert not pid_file.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("attributes_source", ("worktree", "git-directory"))
async def test_repository_process_filters_are_never_executed_or_orphaned(
    tmp_path: Path,
    attributes_source: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _run_fixture_git("init", "--quiet", cwd=project)
    _write_filter_attributes(project, attributes_source=attributes_source)
    _commit_fixture(project)
    sentinel, pid_file = _configure_filter_attack(
        project,
        tmp_path,
        driver_kind="process",
        attributes_source=attributes_source,
    )

    try:
        async with asyncio.timeout(5):
            context = await LocalRepositoryInspector(timeout_seconds=2).inspect(
                project
            )
    finally:
        if pid_file.exists():
            filter_pid = int(pid_file.read_text(encoding="utf-8").strip())
            with pytest.raises(ProcessLookupError):
                os.kill(filter_pid, 0)

    assert context.dirty_paths == (Path("tracked.txt"),)
    assert not sentinel.exists(), "repository-controlled process filter executed"
    assert not pid_file.exists()


@pytest.mark.asyncio
async def test_git_commands_use_immutable_read_only_configuration(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    runner = FakeCommandRunner(root=project)

    await LocalRepositoryInspector(runner, timeout_seconds=10).inspect(project)

    base = (
        "/usr/bin/git",
        "--no-pager",
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "core.excludesFile=/dev/null",
        "-c",
        "submodule.recurse=false",
    )
    trusted = (*base, "-c", f"safe.directory={project}")
    prefix = (
        *trusted,
        f"--git-dir={project / '.git'}",
        f"--work-tree={project}",
    )
    assert runner.calls == [
        (
            *trusted,
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--absolute-git-dir",
        ),
        (*prefix, "branch", "--show-current"),
        (
            *prefix,
            "ls-files",
            "--stage",
            "-t",
            "-z",
            "--full-name",
        ),
        (
            *prefix,
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            "HEAD",
        ),
        (
            *prefix,
            "-c",
            "core.untrackedCache=false",
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            "--full-name",
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config_source",
    ("environment", "global", "system"),
)
async def test_inherited_git_configuration_cannot_execute_fsmonitor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_source: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _run_fixture_git("init", "--quiet", cwd=project)
    sentinel = tmp_path / f"{config_source}-fsmonitor-executed"
    fsmonitor = tmp_path / f"{config_source}-fsmonitor"
    fsmonitor.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' invoked > {shlex.quote(str(sentinel))}\n",
        encoding="utf-8",
    )
    fsmonitor.chmod(0o700)
    config = tmp_path / f"{config_source}.gitconfig"
    config.write_text(f"[core]\n\tfsmonitor = {fsmonitor}\n", encoding="utf-8")
    if config_source == "environment":
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(fsmonitor))
    else:
        monkeypatch.setenv(f"GIT_CONFIG_{config_source.upper()}", str(config))

    await LocalRepositoryInspector(timeout_seconds=10).inspect(project)

    assert not sentinel.exists(), f"inherited {config_source} config executed"


@pytest.mark.asyncio
async def test_included_local_configuration_cannot_execute_fsmonitor(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _run_fixture_git("init", "--quiet", cwd=project)
    sentinel = tmp_path / "included-fsmonitor-executed"
    fsmonitor = tmp_path / "included-fsmonitor"
    fsmonitor.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' invoked > {shlex.quote(str(sentinel))}\n",
        encoding="utf-8",
    )
    fsmonitor.chmod(0o700)
    included = tmp_path / "included.gitconfig"
    included.write_text(f"[core]\n\tfsmonitor = {fsmonitor}\n", encoding="utf-8")
    _run_fixture_git("config", "include.path", str(included), cwd=project)

    await LocalRepositoryInspector(timeout_seconds=10).inspect(project)

    assert not sentinel.exists(), "included repository config executed fsmonitor"


@pytest.mark.asyncio
async def test_git_inspection_does_not_write_index_or_run_index_hook(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _run_fixture_git("init", "--quiet", cwd=project)
    _commit_fixture(project)
    hook_directory = tmp_path / "hooks"
    hook_directory.mkdir()
    sentinel = tmp_path / "post-index-change-executed"
    hook = hook_directory / "post-index-change"
    hook.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' invoked > {shlex.quote(str(sentinel))}\n",
        encoding="utf-8",
    )
    hook.chmod(0o700)
    _run_fixture_git("config", "core.hooksPath", str(hook_directory), cwd=project)
    tracked = project / "tracked.txt"
    tracked.touch()
    index = project / ".git/index"
    before_bytes = index.read_bytes()
    before_stat = index.stat()

    await LocalRepositoryInspector(timeout_seconds=10).inspect(project)

    after_stat = index.stat()
    assert index.read_bytes() == before_bytes
    assert (after_stat.st_mtime_ns, after_stat.st_size) == (
        before_stat.st_mtime_ns,
        before_stat.st_size,
    )
    assert not (project / ".git/index.lock").exists()
    assert not sentinel.exists()


@pytest.mark.asyncio
async def test_linked_worktree_remains_inspectable(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    linked = tmp_path / "linked"
    primary.mkdir()
    _run_fixture_git("init", "--quiet", cwd=primary)
    _commit_fixture(primary)
    _run_fixture_git("worktree", "add", "--quiet", "--detach", str(linked), cwd=primary)
    (linked / "untracked.txt").write_text("untracked\n", encoding="utf-8")

    context = await LocalRepositoryInspector(timeout_seconds=10).inspect(linked)

    assert context.is_git_worktree is True
    assert context.project_path == linked.resolve()
    assert context.dirty_paths == (Path("untracked.txt"),)


@pytest.mark.asyncio
@pytest.mark.parametrize("object_format", ("sha1", "sha256"))
async def test_raw_blob_comparison_supports_repository_object_formats(
    tmp_path: Path,
    object_format: str,
) -> None:
    project = tmp_path / object_format
    project.mkdir()
    _run_fixture_git(
        "init", "--quiet", f"--object-format={object_format}", cwd=project
    )
    _commit_fixture(project)

    clean = await LocalRepositoryInspector(timeout_seconds=10).inspect(project)
    (project / "tracked.txt").write_text("changed\n", encoding="utf-8")
    modified = await LocalRepositoryInspector(timeout_seconds=10).inspect(project)

    assert clean.dirty_paths == ()
    assert modified.dirty_paths == (Path("tracked.txt"),)


@pytest.mark.asyncio
async def test_clean_thousand_file_repository_finishes_within_budget(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _run_fixture_git("init", "--quiet", cwd=project)
    for index in range(1000):
        (project / f"tracked-{index:04d}.txt").write_text(
            f"value-{index:04d}\n", encoding="utf-8"
        )
    _run_fixture_git("add", ".", cwd=project)
    _run_fixture_git(
        "-c",
        "user.name=GoalRouter Test",
        "-c",
        "user.email=goalrouter@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "large fixture",
        cwd=project,
    )

    started = time.monotonic()
    async with asyncio.timeout(30):
        context = await LocalRepositoryInspector(timeout_seconds=120).inspect(project)

    assert time.monotonic() - started < 30
    assert context.dirty_paths == ()


@pytest.mark.asyncio
async def test_missing_committed_head_object_fails_as_corrupt(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _run_fixture_git("init", "--quiet", cwd=project)
    _commit_fixture(project)
    head = (project / ".git/HEAD").read_text(encoding="utf-8")
    assert head.startswith("ref: refs/heads/")
    reference = head.removeprefix("ref: ").strip()
    (project / ".git" / reference).write_text("f" * 40 + "\n", encoding="ascii")

    with pytest.raises(RepositoryError, match=r"(?i)HEAD tree inspection failed"):
        await LocalRepositoryInspector(timeout_seconds=10).inspect(project)


@pytest.mark.asyncio
async def test_composite_dirty_evidence_covers_index_and_worktree_states(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _run_fixture_git("init", "--quiet", cwd=project)
    _commit_fixture(project)
    (project / "deleted.txt").write_text("delete me\n", encoding="utf-8")
    _run_fixture_git("add", "deleted.txt", cwd=project)
    _run_fixture_git(
        "-c",
        "user.name=GoalRouter Test",
        "-c",
        "user.email=goalrouter@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "second fixture",
        cwd=project,
    )
    (project / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (project / "deleted.txt").unlink()
    (project / "staged.txt").write_text("staged\n", encoding="utf-8")
    _run_fixture_git("add", "staged.txt", cwd=project)
    (project / "untracked.txt").write_text("untracked\n", encoding="utf-8")

    context = await LocalRepositoryInspector(timeout_seconds=10).inspect(project)

    assert context.dirty_paths == (
        Path("deleted.txt"),
        Path("staged.txt"),
        Path("tracked.txt"),
        Path("untracked.txt"),
    )


@pytest.mark.asyncio
async def test_symlink_target_and_executable_mode_changes_are_dirty(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _run_fixture_git("init", "--quiet", cwd=project)
    (project / "script.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (project / "link").symlink_to("first-target")
    _run_fixture_git("add", "script.sh", "link", cwd=project)
    _run_fixture_git(
        "-c",
        "user.name=GoalRouter Test",
        "-c",
        "user.email=goalrouter@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "modes",
        cwd=project,
    )
    (project / "script.sh").chmod(0o755)
    (project / "link").unlink()
    (project / "link").symlink_to("other-target")

    context = await LocalRepositoryInspector(timeout_seconds=10).inspect(project)

    assert context.dirty_paths == (Path("link"), Path("script.sh"))


@pytest.mark.asyncio
async def test_skip_worktree_missing_entry_is_not_dirty(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _run_fixture_git("init", "--quiet", cwd=project)
    _commit_fixture(project)
    _run_fixture_git("update-index", "--skip-worktree", "tracked.txt", cwd=project)
    (project / "tracked.txt").unlink()

    context = await LocalRepositoryInspector(timeout_seconds=10).inspect(project)

    assert context.dirty_paths == ()


@pytest.mark.asyncio
async def test_unborn_repository_reports_untracked_paths(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _run_fixture_git("init", "--quiet", cwd=project)
    (project / "new.txt").write_text("new\n", encoding="utf-8")

    context = await LocalRepositoryInspector(timeout_seconds=10).inspect(project)

    assert context.branch is not None
    assert context.dirty_paths == (Path("new.txt"),)


@pytest.mark.asyncio
async def test_tracked_special_file_fails_closed_without_blocking(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _run_fixture_git("init", "--quiet", cwd=project)
    _commit_fixture(project)
    (project / "tracked.txt").unlink()
    os.mkfifo(project / "tracked.txt")

    async with asyncio.timeout(3):
        with pytest.raises(RepositoryError, match=r"(?i)unsafe tracked worktree"):
            await LocalRepositoryInspector(timeout_seconds=1).inspect(project)


@pytest.mark.asyncio
async def test_index_symlink_is_rejected_before_evidence_commands(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _run_fixture_git("init", "--quiet", cwd=project)
    _commit_fixture(project)
    index = project / ".git/index"
    saved_index = project / ".git/index.saved"
    index.rename(saved_index)
    index.symlink_to(saved_index.name)

    with pytest.raises(RepositoryError, match=r"(?i)cannot pin repository evidence"):
        await LocalRepositoryInspector(timeout_seconds=10).inspect(project)


def test_sparse_directory_index_entry_is_rejected() -> None:
    with pytest.raises(RepositoryError, match=r"(?i)unsupported entry mode"):
        repository_module._parse_index_entries(
            f"S 040000 {'a' * 40} 0\tsparse-directory\0"
        )


@pytest.mark.asyncio
async def test_exact_candidate_safe_directory_allows_mounted_ownership_mismatch(
    tmp_path: Path,
) -> None:
    if os.geteuid() != 0:
        pytest.skip("ownership mismatch fixture requires the root test container")
    project = tmp_path / "project"
    project.mkdir()
    _run_fixture_git("init", "--quiet", cwd=project)
    _commit_fixture(project)
    for directory, directories, filenames in (project / ".git").walk(top_down=False):
        for name in filenames:
            os.chown(directory / name, 12345, 12345)
        for name in directories:
            os.chown(directory / name, 12345, 12345)
        os.chown(directory, 12345, 12345)

    context = await LocalRepositoryInspector(timeout_seconds=10).inspect(project)

    assert context.is_git_worktree is True
    assert context.project_path == project.resolve()


@pytest.mark.asyncio
async def test_exact_candidate_trust_does_not_allow_unrelated_owned_repository(
    tmp_path: Path,
) -> None:
    if os.geteuid() != 0:
        pytest.skip("ownership mismatch fixture requires the root test container")
    unrelated = tmp_path / "unrelated"
    project = unrelated / "project"
    unrelated.mkdir()
    _run_fixture_git("init", "--quiet", cwd=unrelated)
    _commit_fixture(unrelated)
    project.mkdir()
    _run_fixture_git("init", "--quiet", cwd=project)
    for directory, directories, filenames in (unrelated / ".git").walk(
        top_down=False
    ):
        for name in filenames:
            os.chown(directory / name, 12345, 12345)
        for name in directories:
            os.chown(directory / name, 12345, 12345)
        os.chown(directory, 12345, 12345)

    class SwapRunner:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []
            self._inner = LocalCommandRunner()

        async def run_read_only(
            self, argv: Sequence[str], *, cwd: Path
        ) -> CommandResult:
            self.calls.append(tuple(argv))
            shutil.rmtree(project / ".git")
            return await self._inner.run_read_only(argv, cwd=cwd)

    runner = SwapRunner()
    with pytest.raises(
        RepositoryError, match=r"(?i)Git repository discovery failed.*exit 128"
    ):
        await LocalRepositoryInspector(runner, timeout_seconds=10).inspect(project)

    assert len(runner.calls) == 1
    assert f"safe.directory={project.resolve()}" in runner.calls[0]
    assert f"safe.directory={unrelated.resolve()}" not in runner.calls[0]


@pytest.mark.asyncio
async def test_discovery_cannot_redirect_exact_safe_directory_to_unrelated_root(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    unrelated = tmp_path / "unrelated"
    project.mkdir()
    unrelated.mkdir()
    (project / ".git").mkdir()
    (unrelated / ".git").mkdir()
    runner = FakeCommandRunner(root=unrelated)

    with pytest.raises(RepositoryError, match=r"(?i)Invalid Git metadata"):
        await LocalRepositoryInspector(runner, timeout_seconds=10).inspect(project)

    assert len(runner.calls) == 1
    assert f"safe.directory={project.resolve()}" in runner.calls[0]
    assert f"safe.directory={unrelated.resolve()}" not in runner.calls[0]


@pytest.mark.asyncio
async def test_status_does_not_enter_submodule_or_execute_its_config(
    tmp_path: Path,
) -> None:
    child = tmp_path / "child"
    parent = tmp_path / "parent"
    child.mkdir()
    parent.mkdir()
    _run_fixture_git("init", "--quiet", cwd=child)
    _commit_fixture(child)
    _run_fixture_git("init", "--quiet", cwd=parent)
    _commit_fixture(parent)
    _run_fixture_git(
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "--quiet",
        str(child),
        "nested",
        cwd=parent,
    )
    _run_fixture_git(
        "-c",
        "user.name=GoalRouter Test",
        "-c",
        "user.email=goalrouter@example.invalid",
        "commit",
        "--quiet",
        "-am",
        "submodule",
        cwd=parent,
    )
    sentinel = tmp_path / "submodule-fsmonitor-executed"
    fsmonitor = tmp_path / "submodule-fsmonitor"
    fsmonitor.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' invoked > {shlex.quote(str(sentinel))}\n",
        encoding="utf-8",
    )
    fsmonitor.chmod(0o700)
    nested = parent / "nested"
    _run_fixture_git("config", "core.fsmonitor", str(fsmonitor), cwd=nested)
    (nested / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    context = await LocalRepositoryInspector(timeout_seconds=10).inspect(parent)

    assert sentinel.exists() is False
    assert Path("nested") not in context.dirty_paths


@pytest.mark.asyncio
async def test_local_command_runner_uses_only_explicit_safe_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class CompletedProcess:
        returncode = 0
        pid = 1
        stdout = _stream()
        stderr = _stream()

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

        async def wait(self) -> int:
            return 0

    async def create_subprocess(*argv: str, **kwargs: object) -> CompletedProcess:
        captured["argv"] = argv
        captured.update(kwargs)
        return CompletedProcess()

    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_CONFIG_PARAMETERS",
        "GIT_EXEC_PATH",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "GIT_TRACE",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
    ):
        monkeypatch.setenv(name, f"DUMMY-{name}")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)

    await LocalCommandRunner().run_read_only(("/usr/bin/git", "status"), cwd=tmp_path)

    assert captured["argv"] == ("/usr/bin/git", "status")
    environment = captured["env"]
    assert environment == {
        "GIT_CONFIG_COUNT": "0",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_EXEC_PATH": "/usr/libexec/git-core",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": "/nonexistent",
        "LC_ALL": "C",
        "PAGER": "cat",
        "PATH": "/usr/bin:/bin",
    }
    assert captured["start_new_session"] is True


@pytest.mark.asyncio
async def test_local_command_runner_wraps_process_start_failure_without_raw_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "DUMMY-SPAWN-FAILURE-DETAIL"

    async def create_subprocess(*argv: str, **kwargs: object) -> None:
        del argv, kwargs
        raise OSError(marker)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)

    with pytest.raises(RepositoryError, match=r"(?i)cannot start Git") as raised:
        await LocalCommandRunner().run_read_only(
            ("/usr/bin/git", "status"), cwd=tmp_path
        )

    assert marker not in str(raised.value)


@pytest.mark.asyncio
async def test_local_command_runner_kills_and_reaps_process_on_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()

    wait_calls = 0

    class BlockingProcess:
        returncode: int | None = None
        pid = 12345
        killed = False
        waited = False
        stdout = _blocking_stream(started)
        stderr = _stream(eof=False)

        async def wait(self) -> int:
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls == 1:
                await asyncio.Event().wait()
            self.waited = True
            self.returncode = -9
            return self.returncode

    process = BlockingProcess()

    async def create_subprocess(*argv: str, **kwargs: object) -> BlockingProcess:
        del argv, kwargs
        return process

    def kill_process_group(pid: int) -> None:
        assert pid == process.pid
        process.killed = True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)
    monkeypatch.setattr(
        repository_module, "_kill_process_group", kill_process_group, raising=False
    )
    task = asyncio.create_task(
        LocalCommandRunner().run_read_only(("/usr/bin/git", "status"), cwd=tmp_path)
    )
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.killed is True
    assert process.waited is True


@pytest.mark.asyncio
async def test_local_command_runner_finishes_reap_after_repeated_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    communication_started = asyncio.Event()
    reap_started = asyncio.Event()
    allow_reap = asyncio.Event()
    wait_calls = 0

    class BlockingProcess:
        returncode: int | None = None
        pid = 23456
        stdout = _blocking_stream(communication_started)
        stderr = _stream(eof=False)

        async def wait(self) -> int:
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls == 1:
                await asyncio.Event().wait()
            reap_started.set()
            await allow_reap.wait()
            self.returncode = -9
            return self.returncode

    process = BlockingProcess()

    async def create_subprocess(*argv: str, **kwargs: object) -> BlockingProcess:
        del argv, kwargs
        return process

    def kill_process_group(pid: int) -> None:
        assert pid == process.pid

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)
    monkeypatch.setattr(
        repository_module, "_kill_process_group", kill_process_group
    )
    task = asyncio.create_task(
        LocalCommandRunner().run_read_only(("/usr/bin/git", "status"), cwd=tmp_path)
    )
    await communication_started.wait()

    task.cancel("first")
    await reap_started.wait()
    task.cancel("second")
    allow_reap.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await task

    assert process.returncode == -9
    assert any(
        "additional cancellation" in note.lower()
        for note in getattr(raised.value, "__notes__", ())
    )


@pytest.mark.asyncio
async def test_local_command_runner_stops_streaming_output_at_the_hard_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed = asyncio.Event()

    class OutputProcess:
        returncode: int | None = None
        pid = 34567
        stdout = _stream(b"x" * 65, eof=False)
        stderr = _stream(eof=False)
        waited = False

        async def wait(self) -> int:
            await killed.wait()
            self.waited = True
            self.returncode = -9
            return self.returncode

    process = OutputProcess()

    async def create_subprocess(*argv: str, **kwargs: object) -> OutputProcess:
        del argv, kwargs
        return process

    def kill_process_group(pid: int) -> None:
        assert pid == process.pid
        killed.set()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)
    monkeypatch.setattr(repository_module, "_kill_process_group", kill_process_group)
    monkeypatch.setattr(repository_module, "_GIT_OUTPUT_LIMIT_BYTES", 64)

    with pytest.raises(RepositoryError, match=r"(?i)output exceeded safe limit"):
        await LocalCommandRunner().run_read_only(
            ("/usr/bin/git", "status"), cwd=tmp_path
        )

    assert killed.is_set()
    assert process.waited is True


@pytest.mark.asyncio
async def test_local_command_runner_preserves_cancellation_during_failed_capture_reap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reap_started = asyncio.Event()
    allow_reap = asyncio.Event()
    wait_calls = 0

    class OutputProcess:
        returncode: int | None = None
        pid = 45678
        stdout = _stream(b"x" * 65, eof=False)
        stderr = _stream(eof=False)

        async def wait(self) -> int:
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls == 1:
                await asyncio.Event().wait()
            reap_started.set()
            await allow_reap.wait()
            self.returncode = -9
            return self.returncode

    process = OutputProcess()

    async def create_subprocess(*argv: str, **kwargs: object) -> OutputProcess:
        del argv, kwargs
        return process

    def kill_process_group(pid: int) -> None:
        assert pid == process.pid

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)
    monkeypatch.setattr(repository_module, "_kill_process_group", kill_process_group)
    monkeypatch.setattr(repository_module, "_GIT_OUTPUT_LIMIT_BYTES", 64)
    task = asyncio.create_task(
        LocalCommandRunner().run_read_only(("/usr/bin/git", "status"), cwd=tmp_path)
    )
    await reap_started.wait()

    task.cancel("first")
    await asyncio.sleep(0)
    task.cancel("second")
    allow_reap.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await task

    assert any(
        "output exceeded safe limit" in note
        for note in getattr(raised.value, "__notes__", ())
    )
    assert any(
        "additional cancellation" in note.lower()
        for note in getattr(raised.value, "__notes__", ())
    )


@pytest.mark.asyncio
async def test_repository_inspector_owns_filesystem_thread_through_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    started = asyncio.Event()
    finished = threading.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = repository_module._resolve_directory

    def blocking_resolve(path: Path) -> Path:
        loop.call_soon_threadsafe(started.set)
        release.wait()
        try:
            return original(path)
        finally:
            finished.set()

    monkeypatch.setattr(repository_module, "_resolve_directory", blocking_resolve)
    task = asyncio.create_task(
        LocalRepositoryInspector(timeout_seconds=10).inspect(project)
    )
    await started.wait()

    task.cancel("first")
    await asyncio.sleep(0)
    completed_early = task.done()
    task.cancel("second")
    release.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await task

    assert completed_early is False
    assert finished.is_set()
    assert any(
        "additional cancellation" in note.lower()
        for note in getattr(raised.value, "__notes__", ())
    )


@pytest.mark.asyncio
async def test_repository_inspector_reaps_filesystem_thread_after_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    finished = threading.Event()
    original = repository_module._resolve_directory

    def slow_resolve(path: Path) -> Path:
        try:
            threading.Event().wait(0.1)
            return original(path)
        finally:
            finished.set()

    monkeypatch.setattr(repository_module, "_resolve_directory", slow_resolve)

    with pytest.raises(RepositoryError, match=r"(?i)(timed out|invalid project path)"):
        await LocalRepositoryInspector(timeout_seconds=0.01).inspect(project)

    assert finished.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failed_phase", "command_token"),
    (
        ("branch", "branch"),
        ("index", "--stage"),
        ("HEAD tree", "ls-tree"),
        ("untracked", "--others"),
    ),
)
async def test_git_phase_runner_error_is_not_exposed_as_exception_group(
    tmp_path: Path,
    failed_phase: str,
    command_token: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()

    class ErrorRunner(FakeCommandRunner):
        async def run_read_only(
            self, argv: Sequence[str], *, cwd: Path
        ) -> CommandResult:
            if command_token in argv:
                raise RepositoryError("DUMMY-INTERNAL-RUNNER-DETAIL")
            return await super().run_read_only(argv, cwd=cwd)

    with pytest.raises(
        RepositoryError, match=rf"(?i)Git {failed_phase} inspection failed"
    ) as raised:
        await LocalRepositoryInspector(
            ErrorRunner(root=project), timeout_seconds=10
        ).inspect(project)

    assert not isinstance(raised.value, ExceptionGroup)
    assert "DUMMY-INTERNAL-RUNNER-DETAIL" not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failed_phase", "command_token"),
    (
        ("branch", "branch"),
        ("index", "--stage"),
        ("HEAD tree", "ls-tree"),
        ("untracked", "--others"),
    ),
)
async def test_nonzero_git_phase_fails_repository_inspection(
    tmp_path: Path,
    failed_phase: str,
    command_token: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()

    class NonzeroRunner(FakeCommandRunner):
        async def run_read_only(
            self, argv: Sequence[str], *, cwd: Path
        ) -> CommandResult:
            if command_token in argv:
                call = tuple(argv)
                self.calls.append(call)
                return CommandResult(call, 129, "", "DUMMY-RAW-DETAIL")
            return await super().run_read_only(argv, cwd=cwd)

    with pytest.raises(
        RepositoryError, match=rf"(?i)Git {failed_phase} inspection failed.*exit 129"
    ) as raised:
        await LocalRepositoryInspector(
            NonzeroRunner(root=project), timeout_seconds=10
        ).inspect(project)

    assert "DUMMY-RAW-DETAIL" not in str(raised.value)


@pytest.mark.asyncio
async def test_git_config_fifo_times_out_and_process_is_reaped(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _run_fixture_git("init", "--quiet", cwd=project)
    included_fifo = tmp_path / "blocked.gitconfig"
    os.mkfifo(included_fifo)
    with (project / ".git/config").open("a", encoding="utf-8") as config:
        config.write(f"\n[include]\n\tpath = {included_fifo}\n")

    async with asyncio.timeout(2):
        with pytest.raises(RepositoryError, match=r"(?i)Git inspection timed out"):
            await LocalRepositoryInspector(timeout_seconds=0.1).inspect(project)


@pytest.mark.asyncio
async def test_git_diagnostics_do_not_echo_repository_controlled_output(
    tmp_path: Path,
) -> None:
    project = tmp_path / "unknown"
    project.mkdir()
    (project / ".git").mkdir()
    marker = "DUMMY-REPOSITORY-CONTROLLED-DIAGNOSTIC"
    runner = FakeCommandRunner(root=project, fail=True, error_detail=marker)

    with pytest.raises(
        RepositoryError, match=r"(?i)Git repository discovery failed.*exit 128"
    ) as raised:
        await LocalRepositoryInspector(runner, timeout_seconds=10).inspect(project)

    assert marker not in str(raised.value)
    assert "not a git worktree" not in str(raised.value)
    assert len(runner.calls) == 1


def test_nul_path_evidence_preserves_unusual_names() -> None:
    dirty = repository_module._parse_nul_paths(
        "line\nfeed\0"
        "tab\tname\0"
        'quote"name\0'
        "literal -> arrow\0"
        " leading-and-trailing  \0"
        "snowman-\u2603\0",
        label="untracked",
    )

    assert dirty == (
        Path("line\nfeed"),
        Path("tab\tname"),
        Path('quote"name'),
        Path("literal -> arrow"),
        Path(" leading-and-trailing  "),
        Path("snowman-\u2603"),
    )


def test_nul_path_evidence_preserves_surrogateescaped_filename_bytes() -> None:
    undecodable = b"invalid-\xff\0".decode("utf-8", errors="surrogateescape")

    dirty = repository_module._parse_nul_paths(undecodable, label="untracked")

    assert len(dirty) == 1
    assert os.fsencode(dirty[0]) == b"invalid-\xff"


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata_type", ("symlink", "fifo"))
async def test_unsafe_git_metadata_fails_before_commands(
    tmp_path: Path,
    metadata_type: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside-git"
    outside.mkdir()
    metadata = project / ".git"
    if metadata_type == "symlink":
        metadata.symlink_to(outside, target_is_directory=True)
    else:
        os.mkfifo(metadata)
    runner = FakeCommandRunner(root=project)

    with pytest.raises(RepositoryError, match=r"(?i)unsafe Git metadata"):
        await LocalRepositoryInspector(runner, timeout_seconds=10).inspect(project)

    assert runner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ("AGENTS.md", "SKILLS.md"))
async def test_rejects_instruction_symlink_that_escapes_repository(
    tmp_path: Path,
    name: str,
) -> None:
    project = tmp_path / "project"
    auth = tmp_path / "codex-auth"
    project.mkdir()
    auth.mkdir()
    marker = "DUMMY-NON-SECRET-MARKER"
    credential = auth / "auth.json"
    credential.write_text(marker, encoding="utf-8")
    (project / name).symlink_to(credential)

    with pytest.raises(
        RepositoryError, match=r"(?i)unsafe repository instruction"
    ) as raised:
        await LocalRepositoryInspector(
            FakeCommandRunner(root=project), timeout_seconds=10
        ).inspect(project)

    assert marker not in str(raised.value)
    assert "type=symlink" in str(raised.value).casefold()
    assert str(credential) not in str(raised.value)


@pytest.mark.asyncio
async def test_rejects_contained_relative_instruction_symlink(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    marker = "DUMMY-CONTAINED-MARKER"
    (project / "instructions.txt").write_text(marker, encoding="utf-8")
    (project / "AGENTS.md").symlink_to("instructions.txt")

    with pytest.raises(
        RepositoryError, match=r"(?i)unsafe repository instruction"
    ) as raised:
        await LocalRepositoryInspector(
            FakeCommandRunner(root=project), timeout_seconds=10
        ).inspect(project)

    assert marker not in str(raised.value)
    assert "type=symlink" in str(raised.value).casefold()
    assert "instructions.txt" not in str(raised.value)


@pytest.mark.asyncio
async def test_rejects_instruction_beneath_swapped_symlink_ancestor(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    target = project / "nested"
    target.mkdir(parents=True)
    (project / ".git").mkdir()
    marker = "DUMMY-ANCESTOR-MARKER"
    (target / "AGENTS.md").write_text(marker, encoding="utf-8")

    def replace_target_with_symlink() -> None:
        real_target = project / "real-nested"
        target.rename(real_target)
        target.symlink_to(real_target, target_is_directory=True)

    runner = MutatingCommandRunner(root=project, mutation=replace_target_with_symlink)

    with pytest.raises(
        RepositoryError, match=r"(?i)unsafe repository instruction"
    ) as raised:
        await LocalRepositoryInspector(runner, timeout_seconds=10).inspect(target)

    assert marker not in str(raised.value)
    assert "type=symlink" in str(raised.value).casefold()


@pytest.mark.asyncio
async def test_rejects_instruction_directory_leaf(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").mkdir()

    with pytest.raises(
        RepositoryError, match=r"(?i)unsafe repository instruction"
    ) as raised:
        await LocalRepositoryInspector(
            FakeCommandRunner(root=project), timeout_seconds=10
        ).inspect(project)

    assert "type=directory" in str(raised.value).casefold()


@pytest.mark.asyncio
async def test_rejects_instruction_fifo_without_blocking(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    fifo = project / "AGENTS.md"
    os.mkfifo(fifo)

    async with asyncio.timeout(2):
        with pytest.raises(
            RepositoryError, match=r"(?i)unsafe repository instruction"
        ) as raised:
            await LocalRepositoryInspector(
                FakeCommandRunner(root=project), timeout_seconds=10
            ).inspect(project)

    assert "type=fifo" in str(raised.value).casefold()


@pytest.mark.asyncio
async def test_rejects_instruction_socket_with_safe_type_label(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    socket_path = project / "AGENTS.md"
    listener = socket.socket(socket.AF_UNIX)
    try:
        listener.bind(str(socket_path))

        with pytest.raises(
            RepositoryError, match=r"(?i)unsafe repository instruction"
        ) as raised:
            await LocalRepositoryInspector(
                FakeCommandRunner(root=project), timeout_seconds=10
            ).inspect(project)
    finally:
        listener.close()

    assert "type=socket" in str(raised.value).casefold()
    assert str(socket_path) in str(raised.value)


def test_fdopen_failure_closes_the_duplicated_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("regular", encoding="utf-8")
    duplicated: list[int] = []
    real_dup = os.dup
    real_fdopen = os.fdopen

    def tracking_dup(descriptor: int) -> int:
        duplicate = real_dup(descriptor)
        duplicated.append(duplicate)
        return duplicate

    def failing_fdopen(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise OSError("synthetic fdopen failure")

    monkeypatch.setattr(
        repository_module, "_duplicate_descriptor", tracking_dup, raising=False
    )
    monkeypatch.setattr(
        repository_module, "_open_descriptor_stream", failing_fdopen, raising=False
    )
    assert os.dup is real_dup
    assert os.fdopen is real_fdopen
    try:
        with pytest.raises(RepositoryError, match=r"(?i)unsafe repository instruction"):
            repository_module._read_optional_instruction(project, Path("AGENTS.md"))

        assert os.dup is real_dup
        assert os.fdopen is real_fdopen
        assert len(duplicated) == 1
        with pytest.raises(OSError) as closed:
            os.fstat(duplicated[0])
        assert closed.value.errno == errno.EBADF
    finally:
        for descriptor in duplicated:
            try:
                os.close(descriptor)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise


def test_fstat_failure_is_wrapped_without_exposing_error_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("regular", encoding="utf-8")
    marker = "DUMMY-FSTAT-DETAIL"

    def failing_fstat(descriptor: int) -> os.stat_result:
        del descriptor
        raise OSError(marker)

    monkeypatch.setattr(
        repository_module, "_fstat_descriptor", failing_fstat, raising=False
    )

    with pytest.raises(
        RepositoryError, match=r"(?i)unsafe repository instruction.*type=unknown"
    ) as raised:
        repository_module._read_optional_instruction(project, Path("AGENTS.md"))

    assert str(project / "AGENTS.md") in str(raised.value)
    assert marker not in str(raised.value)


def test_cleanup_failure_preserves_primary_error_and_attempts_every_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").mkdir()
    calls: list[int] = []
    real_close = os.close

    def close_then_fail_once(descriptor: int) -> None:
        calls.append(descriptor)
        real_close(descriptor)
        if len(calls) == 1:
            raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(
        repository_module, "_close_descriptor", close_then_fail_once, raising=False
    )

    with pytest.raises(RepositoryError, match="type=directory") as raised:
        repository_module._read_optional_instruction(project, Path("AGENTS.md"))

    assert len(calls) == 2
    assert len(set(calls)) == 2
    assert any("synthetic cleanup failure" in note for note in raised.value.__notes__)


def test_cleanup_only_failure_propagates_explicitly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("regular", encoding="utf-8")
    calls: list[int] = []
    real_close = os.close

    def close_then_fail_once(descriptor: int) -> None:
        calls.append(descriptor)
        real_close(descriptor)
        if len(calls) == 1:
            raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(
        repository_module, "_close_descriptor", close_then_fail_once, raising=False
    )

    with pytest.raises(RepositoryError, match=r"(?i)descriptor cleanup failed"):
        repository_module._read_optional_instruction(project, Path("AGENTS.md"))

    assert len(calls) >= 2
    assert len(set(calls)) == len(calls)


@pytest.mark.asyncio
async def test_git_command_failure_is_explicit_evidence(tmp_path: Path) -> None:
    project = tmp_path / "unknown"
    project.mkdir()

    context = await LocalRepositoryInspector(
        FakeCommandRunner(root=project, fail=True), timeout_seconds=10
    ).inspect(project)

    assert context.is_git_worktree is False
    assert context.branch is None
    assert context.dirty_paths == ()
    assert len(context.command_errors) == 3
    assert all("exit 128" in error for error in context.command_errors)
