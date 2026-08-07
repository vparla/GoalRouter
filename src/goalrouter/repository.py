# SPDX-License-Identifier: MIT
# File: src/goalrouter/repository.py
# Purpose: Asynchronous read-only repository evidence discovery

"""Discover repository instructions and status without executing project code."""

import asyncio
import hashlib
import os
import signal
import stat
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from goalrouter.async_tools import prepare_cancellation, wait_for_owned_task
from goalrouter.domain import InstructionFile, RepositoryContext
from goalrouter.errors import RepositoryError

_close_descriptor = os.close
_duplicate_descriptor = os.dup
_fstat_descriptor = os.fstat
_open_descriptor_stream = os.fdopen

_GIT_EXECUTABLE = "/usr/bin/git"
_GIT_ENVIRONMENT = {
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
_GIT_SAFE_PREFIX = (
    _GIT_EXECUTABLE,
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
_GIT_OUTPUT_LIMIT_BYTES = 8 * 1024 * 1024
_GIT_READ_CHUNK_BYTES = 64 * 1024
_HASH_HELPER_ENVIRONMENT = {
    "HOME": "/nonexistent",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin",
}
_BLOB_HASH_BATCH_MAX = 64

_REGULAR_INDEX_MODES = frozenset(("100644", "100755"))
_SYMLINK_INDEX_MODE = "120000"
_GITLINK_INDEX_MODE = "160000"
_ALLOWED_INDEX_MODES = _REGULAR_INDEX_MODES | frozenset(
    (_SYMLINK_INDEX_MODE, _GITLINK_INDEX_MODE)
)


class _GitOutputLimitExceeded(Exception):
    pass


def _kill_process_group(pid: int) -> None:
    os.killpg(pid, signal.SIGKILL)


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Captured result of a read-only command."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class _GitIndexEntry:
    tag: str
    mode: str
    object_id: str
    stage: int
    path: Path


@dataclass(frozen=True, slots=True)
class _GitTreeEntry:
    mode: str
    object_id: str
    path: Path


@dataclass(frozen=True, slots=True)
class _FileFingerprint:
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True, slots=True)
class _PinnedPath:
    path: Path
    descriptor: int | None
    fingerprint: _FileFingerprint | None


@dataclass(frozen=True, slots=True)
class _DirectoryGuard:
    parent_descriptor: int
    name: str
    descriptor: int
    fingerprint: _FileFingerprint


@dataclass(frozen=True, slots=True)
class _OpenedWorktreeEntry:
    kind: str
    mode: str | None
    descriptor: int | None
    fingerprint: _FileFingerprint | None
    symlink_content: bytes | None
    leaf_parent_descriptor: int
    leaf_name: str
    directory_guards: tuple[_DirectoryGuard, ...]
    owned_descriptors: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _BlobHashRequest:
    descriptor: int
    size: int


class CommandRunnerProtocol(Protocol):
    """Port for bounded read-only process execution."""

    async def run_read_only(
        self, argv: Sequence[str], *, cwd: Path
    ) -> CommandResult: ...


class RepositoryInspectorProtocol(Protocol):
    """Port for asynchronous repository evidence discovery."""

    async def inspect(self, project_path: Path) -> RepositoryContext: ...


class BlobHasherProtocol(Protocol):
    async def hash_descriptors(
        self,
        requests: Sequence[_BlobHashRequest],
        *,
        algorithm: str,
    ) -> tuple[str, ...]: ...


class _DigestProtocol(Protocol):
    def update(self, data: bytes) -> None: ...

    def hexdigest(self) -> str: ...


class LocalCommandRunner:
    """Run argv directly without a shell and capture UTF-8 output."""

    async def run_read_only(self, argv: Sequence[str], *, cwd: Path) -> CommandResult:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=_GIT_ENVIRONMENT,
                start_new_session=True,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise RepositoryError("Cannot start Git repository inspection") from error
        communication = asyncio.create_task(_collect_process_output(process))
        try:
            stdout, stderr = await asyncio.shield(communication)
        except BaseException as error:
            cancellations, cleanup_error = await _stop_and_reap_process(
                process, communication
            )
            if isinstance(error, asyncio.CancelledError):
                if cleanup_error is not None:
                    error.add_note(f"Git subprocess cleanup failed: {cleanup_error}")
                cancellation = prepare_cancellation(
                    (error, *cancellations), operation="Git subprocess cleanup"
                )
                raise cancellation from None
            mapped = _map_git_capture_error(error)
            if cancellations:
                cancellation = prepare_cancellation(
                    cancellations, operation="Git subprocess cleanup"
                )
                cancellation.add_note(
                    f"Git subprocess capture failed before cancellation: {mapped}"
                )
                if cleanup_error is not None:
                    cancellation.add_note(
                        f"Git subprocess cleanup failed: {cleanup_error}"
                    )
                raise cancellation from None
            if cleanup_error is not None:
                mapped.add_note(f"Git subprocess cleanup failed: {cleanup_error}")
            raise mapped from None
        return CommandResult(
            argv=tuple(argv),
            returncode=process.returncode or 0,
            stdout=stdout.decode("utf-8", errors="surrogateescape"),
            stderr=stderr.decode("utf-8", errors="surrogateescape"),
        )


class LocalBlobHasher:
    """Hash a bounded descriptor batch in one killable owned subprocess."""

    async def hash_descriptors(
        self,
        requests: Sequence[_BlobHashRequest],
        *,
        algorithm: str,
    ) -> tuple[str, ...]:
        batch = tuple(requests)
        if (
            not batch
            or len(batch) > _BLOB_HASH_BATCH_MAX
            or algorithm not in {"sha1", "sha256"}
            or any(request.descriptor < 0 or request.size < 0 for request in batch)
        ):
            raise RepositoryError("Invalid tracked worktree hashing request")
        arguments = tuple(
            value
            for request in batch
            for value in (str(request.descriptor), str(request.size))
        )
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-B",
                "-m",
                "goalrouter.hash_helper",
                algorithm,
                *arguments,
                cwd=Path("/"),
                env=_HASH_HELPER_ENVIRONMENT,
                pass_fds=tuple(request.descriptor for request in batch),
                start_new_session=True,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise RepositoryError("Cannot start tracked worktree hashing") from error
        communication = asyncio.create_task(_collect_process_output(process))
        try:
            stdout, stderr = await asyncio.shield(communication)
        except BaseException as error:
            cancellations, cleanup_error = await _stop_and_reap_process(
                process, communication
            )
            if isinstance(error, asyncio.CancelledError):
                if cleanup_error is not None:
                    error.add_note(
                        f"Worktree hash subprocess cleanup failed: {cleanup_error}"
                    )
                cancellation = prepare_cancellation(
                    (error, *cancellations),
                    operation="Worktree hash subprocess cleanup",
                )
                raise cancellation from None
            mapped = RepositoryError("Cannot capture tracked worktree hash")
            if cancellations:
                cancellation = prepare_cancellation(
                    cancellations,
                    operation="Worktree hash subprocess cleanup",
                )
                cancellation.add_note(
                    "Worktree hash capture failed before cancellation"
                )
                raise cancellation from None
            if cleanup_error is not None:
                mapped.add_note(
                    f"Worktree hash subprocess cleanup failed: {cleanup_error}"
                )
            raise mapped from None
        expected_length = 40 if algorithm == "sha1" else 64
        try:
            output = stdout.decode("ascii")
        except UnicodeError as error:
            raise RepositoryError("Tracked worktree hashing returned invalid output") from error
        digests = tuple(output.splitlines())
        if (
            process.returncode != 0
            or stderr
            or len(digests) != len(batch)
            or any(len(digest) != expected_length for digest in digests)
            or any(
                character not in "0123456789abcdef"
                for digest in digests
                for character in digest
            )
            or output != "".join(f"{digest}\n" for digest in digests)
        ):
            raise RepositoryError("Tracked worktree hashing failed")
        return digests


async def _capture_git_command(
    runner: CommandRunnerProtocol,
    argv: Sequence[str],
    *,
    cwd: Path,
) -> CommandResult | RepositoryError:
    try:
        return await runner.run_read_only(argv, cwd=cwd)
    except RepositoryError as error:
        return error


def _require_git_command(
    label: str,
    outcome: CommandResult | RepositoryError,
) -> CommandResult:
    if isinstance(outcome, RepositoryError):
        raise RepositoryError(f"Git {label} inspection failed") from outcome
    if outcome.returncode != 0:
        raise RepositoryError(
            f"Git {label} inspection failed (exit {outcome.returncode})"
        )
    return outcome


async def _collect_process_output(
    process: asyncio.subprocess.Process,
) -> tuple[bytes, bytes]:
    if process.stdout is None or process.stderr is None:
        raise RepositoryError("Git subprocess capture pipes are unavailable")
    async with asyncio.TaskGroup() as group:
        stdout_task = group.create_task(_read_bounded_stream(process.stdout))
        stderr_task = group.create_task(_read_bounded_stream(process.stderr))
        group.create_task(process.wait())
    return stdout_task.result(), stderr_task.result()


async def _read_bounded_stream(stream: asyncio.StreamReader) -> bytes:
    output = bytearray()
    while chunk := await stream.read(_GIT_READ_CHUNK_BYTES):
        if len(output) + len(chunk) > _GIT_OUTPUT_LIMIT_BYTES:
            raise _GitOutputLimitExceeded
        output.extend(chunk)
    return bytes(output)


def _map_git_capture_error(error: BaseException) -> RepositoryError:
    if isinstance(error, BaseExceptionGroup):
        if error.subgroup(_GitOutputLimitExceeded) is not None:
            return RepositoryError("Repository Git inspection output exceeded safe limit")
    elif isinstance(error, _GitOutputLimitExceeded):
        return RepositoryError("Repository Git inspection output exceeded safe limit")
    if isinstance(error, RepositoryError):
        return error
    return RepositoryError("Cannot capture Git repository inspection output")


async def _stop_and_reap_process(
    process: asyncio.subprocess.Process,
    communication: asyncio.Task[tuple[bytes, bytes]],
) -> tuple[tuple[asyncio.CancelledError, ...], BaseException | None]:
    cleanup_error: BaseException | None = None
    if process.returncode is None:
        try:
            _kill_process_group(process.pid)
        except ProcessLookupError:
            pass
        except BaseException as error:
            cleanup_error = error
    communication.cancel()
    communication_cancellations = await wait_for_owned_task(communication)
    try:
        communication.result()
    except asyncio.CancelledError:
        pass
    except BaseException:
        pass
    reap = asyncio.create_task(process.wait())
    reap_cancellations = await wait_for_owned_task(reap)
    try:
        reap.result()
    except BaseException as error:
        if cleanup_error is None:
            cleanup_error = error
        else:
            cleanup_error.add_note(f"Git process reap also failed: {error}")
    return (*communication_cancellations, *reap_cancellations), cleanup_error


async def _run_filesystem_operation[T](
    operation: Callable[..., T],
    *args: object,
) -> T:
    owned_operation = asyncio.create_task(asyncio.to_thread(operation, *args))
    cancellations = await wait_for_owned_task(owned_operation)
    try:
        result = owned_operation.result()
    except BaseException as error:
        if cancellations:
            cancellation = prepare_cancellation(
                cancellations, operation="filesystem inspection"
            )
            cancellation.add_note(
                "Filesystem inspection failed after cancellation was requested"
            )
            raise cancellation from error
        raise
    if cancellations:
        raise prepare_cancellation(
            cancellations, operation="filesystem inspection"
        ) from None
    return result


class LocalRepositoryInspector:
    """Collect Git, instruction, language, and Docker lifecycle evidence."""

    def __init__(
        self,
        runner: CommandRunnerProtocol | None = None,
        hasher: BlobHasherProtocol | None = None,
        *,
        timeout_seconds: float,
    ) -> None:
        self._runner = runner or LocalCommandRunner()
        self._hasher = hasher or LocalBlobHasher()
        self._timeout_seconds = timeout_seconds

    async def inspect(self, project_path: Path) -> RepositoryContext:
        """Inspect an absolute directory without mutating or executing it."""

        if not project_path.is_absolute():
            raise RepositoryError(f"Project path must be absolute: {project_path}")
        try:
            async with asyncio.timeout(self._timeout_seconds):
                target = await _run_filesystem_operation(
                    _resolve_directory, project_path
                )
        except (OSError, TimeoutError) as error:
            raise RepositoryError(f"Invalid project path {project_path}: {error}") from error

        try:
            async with asyncio.timeout(self._timeout_seconds):
                candidate_root, git_metadata = await _run_filesystem_operation(
                    _discover_repository_candidate, target
                )
                evidence = await _run_filesystem_operation(
                    _read_filesystem_evidence, candidate_root, target
                )
        except TimeoutError as error:
            raise RepositoryError(
                f"Repository filesystem inspection timed out for {target}"
            ) from error

        if git_metadata is None:
            return _non_git_context(target, evidence)

        trusted_prefix = (
            *_GIT_SAFE_PREFIX,
            "-c",
            f"safe.directory={candidate_root}",
        )
        discovery_argv = (
            *trusted_prefix,
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--absolute-git-dir",
        )
        try:
            async with asyncio.timeout(self._timeout_seconds):
                discovery_result = await self._runner.run_read_only(
                    discovery_argv, cwd=target
                )
        except TimeoutError as error:
            raise RepositoryError(f"Repository Git inspection timed out for {target}") from error

        if discovery_result.returncode != 0:
            raise RepositoryError(
                "Git repository discovery failed for "
                f"{target} (exit {discovery_result.returncode})"
            )

        try:
            repository_root, git_directory = await _run_filesystem_operation(
                _resolve_git_discovery,
                discovery_result.stdout,
                candidate_root,
                target,
            )
        except OSError as error:
            raise RepositoryError(f"Invalid Git metadata for {target}: {error}") from error

        pinned_prefix = (
            *trusted_prefix,
            f"--git-dir={git_directory}",
            f"--work-tree={repository_root}",
        )
        try:
            async with asyncio.timeout(self._timeout_seconds):
                root_pin = await _run_filesystem_operation(
                    _pin_required_directory, repository_root
                )
                index_pin = await _run_filesystem_operation(
                    _pin_optional_regular_file, git_directory / "index"
                )
        except (OSError, TimeoutError) as error:
            raise RepositoryError(
                f"Cannot pin repository evidence for {target}"
            ) from error

        primary_error: BaseException | None = None
        try:
            try:
                async with asyncio.timeout(self._timeout_seconds):
                    async with asyncio.TaskGroup() as group:
                        branch_task = group.create_task(
                            _capture_git_command(
                                self._runner,
                                (*pinned_prefix, "branch", "--show-current"),
                                cwd=target,
                            )
                        )
                        index_task = group.create_task(
                            _capture_git_command(
                                self._runner,
                                (
                                    *pinned_prefix,
                                    "ls-files",
                                    "--stage",
                                    "-t",
                                    "-z",
                                    "--full-name",
                                ),
                                cwd=target,
                            )
                        )
                        head_task = group.create_task(
                            _capture_git_command(
                                self._runner,
                                (
                                    *pinned_prefix,
                                    "ls-tree",
                                    "-r",
                                    "-z",
                                    "--full-tree",
                                    "HEAD",
                                ),
                                cwd=target,
                            )
                        )
                        untracked_task = group.create_task(
                            _capture_git_command(
                                self._runner,
                                (
                                    *pinned_prefix,
                                    "-c",
                                    "core.untrackedCache=false",
                                    "ls-files",
                                    "--others",
                                    "--exclude-standard",
                                    "-z",
                                    "--full-name",
                                ),
                                cwd=target,
                            )
                        )
            except TimeoutError as error:
                raise RepositoryError(
                    f"Repository Git inspection timed out for {target}"
                ) from error

            branch_result = _require_git_command("branch", branch_task.result())
            index_result = _require_git_command("index", index_task.result())
            untracked_result = _require_git_command(
                "untracked", untracked_task.result()
            )
            branch = _parse_branch(branch_result.stdout)
            head_stdout = await _resolve_head_tree(
                head_task.result(),
                branch,
                runner=self._runner,
                pinned_prefix=pinned_prefix,
                cwd=target,
                timeout_seconds=self._timeout_seconds,
            )
            index_entries = _parse_index_entries(index_result.stdout)
            head_entries = _parse_tree_entries(head_stdout)
            untracked_paths = _parse_nul_paths(
                untracked_result.stdout, label="untracked"
            )

            if root_pin.descriptor is None:
                raise RepositoryError("Repository root descriptor is unavailable")
            try:
                async with asyncio.timeout(self._timeout_seconds):
                    dirty_paths = await _collect_dirty_paths(
                        root_pin.descriptor,
                        index_entries,
                        head_entries,
                        untracked_paths,
                        self._hasher,
                    )
                    evidence = await _run_filesystem_operation(
                        _read_filesystem_evidence, repository_root, target
                    )
                    await _run_filesystem_operation(_validate_pinned_path, root_pin)
                    await _run_filesystem_operation(_validate_pinned_path, index_pin)
            except TimeoutError as error:
                raise RepositoryError(
                    f"Repository filesystem inspection timed out for {target}"
                ) from error
            except OSError as error:
                raise RepositoryError(
                    f"Repository evidence changed during inspection for {target}"
                ) from error

            return RepositoryContext(
                project_path=target,
                is_git_worktree=True,
                branch=branch,
                dirty_paths=dirty_paths,
                instruction_files=evidence.instruction_files,
                language_counts=evidence.language_counts,
                docker_files=evidence.docker_files,
                command_errors=(),
            )
        except BaseException as error:
            primary_error = error
            raise
        finally:
            cleanup_error = _close_pinned_paths((index_pin, root_pin))
            if cleanup_error is not None:
                if primary_error is not None:
                    primary_error.add_note(
                        f"Repository evidence descriptor cleanup failed: {cleanup_error}"
                    )
                else:
                    raise RepositoryError(
                        "Repository evidence descriptor cleanup failed"
                    ) from cleanup_error


def _parse_branch(stdout: str) -> str | None:
    if "\0" in stdout or stdout.count("\n") > 1:
        raise RepositoryError("Git branch inspection returned malformed evidence")
    branch = stdout.removesuffix("\n")
    if "\r" in branch:
        raise RepositoryError("Git branch inspection returned malformed evidence")
    return branch or None


async def _resolve_head_tree(
    outcome: CommandResult | RepositoryError,
    branch: str | None,
    *,
    runner: CommandRunnerProtocol,
    pinned_prefix: tuple[str, ...],
    cwd: Path,
    timeout_seconds: float,
) -> str:
    if isinstance(outcome, RepositoryError):
        raise RepositoryError("Git HEAD tree inspection failed") from outcome
    if outcome.returncode == 0:
        return outcome.stdout
    if outcome.returncode == 128 and branch is not None and not outcome.stdout:
        try:
            async with asyncio.timeout(timeout_seconds):
                ref_result = await runner.run_read_only(
                    (
                        *pinned_prefix,
                        "show-ref",
                        "--verify",
                        "--quiet",
                        f"refs/heads/{branch}",
                    ),
                    cwd=cwd,
                )
        except TimeoutError as error:
            raise RepositoryError("Git unborn-HEAD inspection timed out") from error
        except RepositoryError as error:
            raise RepositoryError("Git unborn-HEAD inspection failed") from error
        if (
            ref_result.returncode == 1
            and not ref_result.stdout
            and not ref_result.stderr
        ):
            return ""
    raise RepositoryError(
        f"Git HEAD tree inspection failed (exit {outcome.returncode})"
    )


def _parse_index_entries(stdout: str) -> tuple[_GitIndexEntry, ...]:
    records = _split_nul_records(stdout, label="index")
    entries: list[_GitIndexEntry] = []
    object_lengths: set[int] = set()
    for record in records:
        try:
            metadata, raw_path = record.split("\t", 1)
            tag, mode, object_id, raw_stage = metadata.split(" ")
            stage = int(raw_stage)
        except (ValueError, TypeError) as error:
            raise RepositoryError("Git index returned malformed evidence") from error
        if tag not in {"H", "S", "M"}:
            raise RepositoryError("Git index returned an unknown entry tag")
        if mode not in _ALLOWED_INDEX_MODES:
            raise RepositoryError("Git index returned an unsupported entry mode")
        if not 0 <= stage <= 3 or (tag == "M") != (stage != 0):
            raise RepositoryError("Git index returned an invalid stage entry")
        _validate_object_id(object_id, object_lengths)
        entries.append(
            _GitIndexEntry(
                tag=tag,
                mode=mode,
                object_id=object_id,
                stage=stage,
                path=_parse_repository_path(raw_path, label="index"),
            )
        )
    _require_consistent_object_format(object_lengths)
    return tuple(entries)


def _parse_tree_entries(stdout: str) -> tuple[_GitTreeEntry, ...]:
    records = _split_nul_records(stdout, label="HEAD tree")
    entries: list[_GitTreeEntry] = []
    seen: set[Path] = set()
    object_lengths: set[int] = set()
    for record in records:
        try:
            metadata, raw_path = record.split("\t", 1)
            mode, object_type, object_id = metadata.split(" ")
        except ValueError as error:
            raise RepositoryError("Git HEAD tree returned malformed evidence") from error
        expected_type = "commit" if mode == _GITLINK_INDEX_MODE else "blob"
        if mode not in _ALLOWED_INDEX_MODES or object_type != expected_type:
            raise RepositoryError("Git HEAD tree returned unsupported evidence")
        _validate_object_id(object_id, object_lengths)
        path = _parse_repository_path(raw_path, label="HEAD tree")
        if path in seen:
            raise RepositoryError("Git HEAD tree returned duplicate path evidence")
        seen.add(path)
        entries.append(_GitTreeEntry(mode=mode, object_id=object_id, path=path))
    _require_consistent_object_format(object_lengths)
    return tuple(entries)


def _parse_nul_paths(stdout: str, *, label: str) -> tuple[Path, ...]:
    paths = tuple(
        _parse_repository_path(record, label=label)
        for record in _split_nul_records(stdout, label=label)
    )
    if len(set(paths)) != len(paths):
        raise RepositoryError(f"Git {label} returned duplicate path evidence")
    return paths


def _split_nul_records(stdout: str, *, label: str) -> tuple[str, ...]:
    if not stdout:
        return ()
    if not stdout.endswith("\0"):
        raise RepositoryError(
            f"Git {label} returned malformed NUL-delimited evidence"
        )
    records = tuple(stdout.split("\0")[:-1])
    if any(not record for record in records):
        raise RepositoryError(f"Git {label} returned an empty evidence record")
    return records


def _parse_repository_path(raw_path: str, *, label: str) -> Path:
    path = Path(raw_path)
    if (
        not raw_path
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise RepositoryError(f"Git {label} returned an unsafe path")
    return path


def _validate_object_id(object_id: str, lengths: set[int]) -> None:
    if len(object_id) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in object_id
    ):
        raise RepositoryError("Git returned an invalid object identifier")
    lengths.add(len(object_id))


def _require_consistent_object_format(lengths: set[int]) -> None:
    if len(lengths) > 1:
        raise RepositoryError("Git returned mixed object identifier formats")


async def _collect_dirty_paths(
    root_descriptor: int,
    index_entries: tuple[_GitIndexEntry, ...],
    head_entries: tuple[_GitTreeEntry, ...],
    untracked_paths: tuple[Path, ...],
    hasher: BlobHasherProtocol,
) -> tuple[Path, ...]:
    object_lengths = {len(entry.object_id) for entry in index_entries}
    object_lengths.update(len(entry.object_id) for entry in head_entries)
    _require_consistent_object_format(object_lengths)
    algorithm = "sha256" if object_lengths == {64} else "sha1"
    index_by_path: dict[Path, list[_GitIndexEntry]] = {}
    for entry in index_entries:
        index_by_path.setdefault(entry.path, []).append(entry)
    head_by_path = {entry.path: entry for entry in head_entries}
    dirty = set(untracked_paths)
    worktree_entries: list[_GitIndexEntry] = []

    for path in sorted(
        set(index_by_path) | set(head_by_path), key=lambda item: item.as_posix()
    ):
        candidates = index_by_path.get(path, [])
        stages = [entry.stage for entry in candidates]
        if len(stages) != len(set(stages)) or (0 in stages and len(stages) != 1):
            raise RepositoryError("Git index returned conflicting stage evidence")
        if len(candidates) != 1 or candidates[0].stage != 0:
            dirty.add(path)
            continue
        index_entry = candidates[0]
        head_entry = head_by_path.get(path)
        if (
            head_entry is None
            or head_entry.mode != index_entry.mode
            or head_entry.object_id != index_entry.object_id
        ):
            dirty.add(path)
        if index_entry.mode == _GITLINK_INDEX_MODE or index_entry.tag == "S":
            continue
        worktree_entries.append(index_entry)

    for offset in range(0, len(worktree_entries), _BLOB_HASH_BATCH_MAX):
        dirty.update(
            await _compare_worktree_batch(
                root_descriptor,
                tuple(worktree_entries[offset : offset + _BLOB_HASH_BATCH_MAX]),
                algorithm=algorithm,
                hasher=hasher,
            )
        )

    return tuple(sorted(dirty, key=lambda path: path.as_posix()))


async def _compare_worktree_batch(
    root_descriptor: int,
    entries: tuple[_GitIndexEntry, ...],
    *,
    algorithm: str,
    hasher: BlobHasherProtocol,
) -> set[Path]:
    opened_entries: list[tuple[_GitIndexEntry, _OpenedWorktreeEntry]] = []
    primary_error: BaseException | None = None
    try:
        for entry in entries:
            opened_entries.append(  # noqa: PERF401 - retain partial opens for cleanup
                (
                    entry,
                    await _run_filesystem_operation(
                        _open_worktree_entry,
                        root_descriptor,
                        entry.path,
                        entry.mode,
                    ),
                )
            )
        dirty: set[Path] = set()
        regular_entries: list[tuple[_GitIndexEntry, _OpenedWorktreeEntry]] = []
        for entry, opened in opened_entries:
            if opened.kind != "matched" or opened.mode != entry.mode:
                await _run_filesystem_operation(
                    _validate_opened_worktree_entry, opened
                )
                dirty.add(entry.path)
            elif opened.symlink_content is not None:
                if _hash_git_blob(algorithm, opened.symlink_content) != entry.object_id:
                    dirty.add(entry.path)
                await _run_filesystem_operation(
                    _validate_opened_worktree_entry, opened
                )
            else:
                if opened.descriptor is None or opened.fingerprint is None:
                    raise RepositoryError("Tracked worktree descriptor is unavailable")
                regular_entries.append((entry, opened))
        if regular_entries:
            digests = await hasher.hash_descriptors(
                tuple(
                    _BlobHashRequest(
                        descriptor=opened.descriptor,
                        size=opened.fingerprint.size,
                    )
                    for _entry, opened in regular_entries
                    if opened.descriptor is not None and opened.fingerprint is not None
                ),
                algorithm=algorithm,
            )
            if len(digests) != len(regular_entries):
                raise RepositoryError("Tracked worktree hashing returned wrong batch size")
            for (entry, opened), object_id in zip(
                regular_entries, digests, strict=True
            ):
                await _run_filesystem_operation(
                    _validate_opened_worktree_entry, opened
                )
                if object_id != entry.object_id:
                    dirty.add(entry.path)
        return dirty
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_error: OSError | None = None
        for _entry, opened in reversed(opened_entries):
            current_error = _close_descriptors(list(opened.owned_descriptors))
            if cleanup_error is None and current_error is not None:
                cleanup_error = current_error
        if cleanup_error is not None:
            if primary_error is not None:
                primary_error.add_note(
                    f"Tracked worktree descriptor cleanup failed: {cleanup_error}"
                )
            else:
                raise RepositoryError(
                    "Tracked worktree descriptor cleanup failed"
                ) from cleanup_error


def _new_object_digest(algorithm: str) -> _DigestProtocol:
    if algorithm == "sha1":
        return hashlib.sha1(usedforsecurity=False)
    if algorithm == "sha256":
        return hashlib.sha256(usedforsecurity=False)
    raise RepositoryError("Git returned an unsupported object format")


def _hash_git_blob(algorithm: str, content: bytes) -> str:
    digest = _new_object_digest(algorithm)
    digest.update(f"blob {len(content)}\0".encode("ascii"))
    digest.update(content)
    return digest.hexdigest()


def _open_worktree_entry(
    root_descriptor: int,
    path: Path,
    expected_mode: str,
) -> _OpenedWorktreeEntry:
    descriptors = [_duplicate_descriptor(root_descriptor)]
    guards: list[_DirectoryGuard] = []
    current = descriptors[0]
    try:
        for part in path.parts[:-1]:
            try:
                before = os.stat(part, dir_fd=current, follow_symlinks=False)
            except FileNotFoundError:
                return _opened_nonmatching_entry(
                    current, part, guards, descriptors, fingerprint=None
                )
            entry_type = _mode_type(before.st_mode)
            if entry_type == "symlink" or entry_type not in {"directory", "regular"}:
                raise RepositoryError("Unsafe tracked worktree path boundary")
            if entry_type == "regular":
                return _opened_nonmatching_entry(
                    current,
                    part,
                    guards,
                    descriptors,
                    fingerprint=_fingerprint(before),
                )
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=current,
            )
            descriptors.append(child)
            child_fingerprint = _fingerprint(os.fstat(child))
            if child_fingerprint != _fingerprint(before):
                raise RepositoryError("Tracked worktree path changed during inspection")
            guards.append(
                _DirectoryGuard(
                    parent_descriptor=current,
                    name=part,
                    descriptor=child,
                    fingerprint=child_fingerprint,
                )
            )
            current = child

        leaf_name = path.parts[-1]
        try:
            before = os.stat(leaf_name, dir_fd=current, follow_symlinks=False)
        except FileNotFoundError:
            return _opened_nonmatching_entry(
                current, leaf_name, guards, descriptors, fingerprint=None
            )
        leaf_type = _mode_type(before.st_mode)
        before_fingerprint = _fingerprint(before)
        if leaf_type in {"fifo", "socket", "block-device", "character-device", "unknown"}:
            raise RepositoryError("Unsafe tracked worktree entry type")
        if leaf_type == "directory":
            return _opened_nonmatching_entry(
                current,
                leaf_name,
                guards,
                descriptors,
                fingerprint=before_fingerprint,
            )
        if leaf_type == "symlink":
            if expected_mode != _SYMLINK_INDEX_MODE:
                return _opened_nonmatching_entry(
                    current,
                    leaf_name,
                    guards,
                    descriptors,
                    fingerprint=before_fingerprint,
                )
            content = os.fsencode(os.readlink(leaf_name, dir_fd=current))
            after = os.stat(leaf_name, dir_fd=current, follow_symlinks=False)
            if _fingerprint(after) != before_fingerprint:
                raise RepositoryError("Tracked worktree path changed during inspection")
            return _OpenedWorktreeEntry(
                kind="matched",
                mode=_SYMLINK_INDEX_MODE,
                descriptor=None,
                fingerprint=before_fingerprint,
                symlink_content=content,
                leaf_parent_descriptor=current,
                leaf_name=leaf_name,
                directory_guards=tuple(guards),
                owned_descriptors=tuple(descriptors),
            )
        if expected_mode not in _REGULAR_INDEX_MODES:
            return _opened_nonmatching_entry(
                current,
                leaf_name,
                guards,
                descriptors,
                fingerprint=before_fingerprint,
            )
        leaf = os.open(
            leaf_name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=current,
        )
        descriptors.append(leaf)
        leaf_fingerprint = _fingerprint(os.fstat(leaf))
        if leaf_fingerprint != before_fingerprint:
            raise RepositoryError("Tracked worktree path changed during inspection")
        worktree_mode = (
            "100755" if leaf_fingerprint.mode & stat.S_IXUSR else "100644"
        )
        return _OpenedWorktreeEntry(
            kind="matched",
            mode=worktree_mode,
            descriptor=leaf,
            fingerprint=leaf_fingerprint,
            symlink_content=None,
            leaf_parent_descriptor=current,
            leaf_name=leaf_name,
            directory_guards=tuple(guards),
            owned_descriptors=tuple(descriptors),
        )
    except BaseException:
        _close_descriptors(descriptors)
        raise


def _opened_nonmatching_entry(
    parent_descriptor: int,
    name: str,
    guards: list[_DirectoryGuard],
    descriptors: list[int],
    *,
    fingerprint: _FileFingerprint | None,
) -> _OpenedWorktreeEntry:
    return _OpenedWorktreeEntry(
        kind="mismatch",
        mode=None,
        descriptor=None,
        fingerprint=fingerprint,
        symlink_content=None,
        leaf_parent_descriptor=parent_descriptor,
        leaf_name=name,
        directory_guards=tuple(guards),
        owned_descriptors=tuple(descriptors),
    )


def _validate_opened_worktree_entry(opened: _OpenedWorktreeEntry) -> None:
    for guard in opened.directory_guards:
        if _fingerprint(os.fstat(guard.descriptor)) != guard.fingerprint:
            raise OSError("tracked directory descriptor changed")
        current = os.stat(
            guard.name,
            dir_fd=guard.parent_descriptor,
            follow_symlinks=False,
        )
        if _fingerprint(current) != guard.fingerprint:
            raise OSError("tracked directory entry changed")
    if opened.descriptor is not None:
        if opened.fingerprint is None:
            raise OSError("tracked file fingerprint missing")
        if _fingerprint(os.fstat(opened.descriptor)) != opened.fingerprint:
            raise OSError("tracked file descriptor changed")
    try:
        current_leaf = os.stat(
            opened.leaf_name,
            dir_fd=opened.leaf_parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        if opened.fingerprint is None:
            return
        raise OSError("tracked worktree entry disappeared") from None
    if opened.fingerprint is None or _fingerprint(current_leaf) != opened.fingerprint:
        raise OSError("tracked worktree entry changed")


def _fingerprint(value: os.stat_result) -> _FileFingerprint:
    return _FileFingerprint(
        device=value.st_dev,
        inode=value.st_ino,
        mode=value.st_mode,
        size=value.st_size,
        modified_ns=value.st_mtime_ns,
        changed_ns=value.st_ctime_ns,
    )


def _pin_required_directory(path: Path) -> _PinnedPath:
    descriptor = os.open(
        path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    )
    try:
        fingerprint = _fingerprint(os.fstat(descriptor))
        current = _fingerprint(path.stat(follow_symlinks=False))
        if fingerprint != current or not stat.S_ISDIR(fingerprint.mode):
            raise OSError("repository directory changed")
        return _PinnedPath(path=path, descriptor=descriptor, fingerprint=fingerprint)
    except BaseException:
        _close_descriptor(descriptor)
        raise


def _pin_optional_regular_file(path: Path) -> _PinnedPath:
    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW
        )
    except FileNotFoundError:
        return _PinnedPath(path=path, descriptor=None, fingerprint=None)
    try:
        fingerprint = _fingerprint(os.fstat(descriptor))
        current = _fingerprint(path.stat(follow_symlinks=False))
        if fingerprint != current or not stat.S_ISREG(fingerprint.mode):
            raise OSError("repository index is not a stable regular file")
        return _PinnedPath(path=path, descriptor=descriptor, fingerprint=fingerprint)
    except BaseException:
        _close_descriptor(descriptor)
        raise


def _validate_pinned_path(pin: _PinnedPath) -> None:
    if pin.descriptor is None:
        try:
            pin.path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return
        raise OSError("optional repository evidence appeared during inspection")
    if pin.fingerprint is None:
        raise OSError("repository evidence fingerprint missing")
    if _fingerprint(os.fstat(pin.descriptor)) != pin.fingerprint:
        raise OSError("repository evidence descriptor changed")
    if _fingerprint(pin.path.stat(follow_symlinks=False)) != pin.fingerprint:
        raise OSError("repository evidence path changed")


def _close_pinned_paths(pins: tuple[_PinnedPath, ...]) -> OSError | None:
    descriptors = [pin.descriptor for pin in pins if pin.descriptor is not None]
    return _close_descriptors(descriptors)


@dataclass(frozen=True, slots=True)
class _FilesystemEvidence:
    instruction_files: tuple[InstructionFile, ...]
    language_counts: tuple[tuple[str, int], ...]
    docker_files: tuple[Path, ...]


def _resolve_directory(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise NotADirectoryError(resolved)
    return resolved


def _discover_repository_candidate(target: Path) -> tuple[Path, Path | None]:
    for candidate in (target, *target.parents):
        metadata = candidate / ".git"
        try:
            metadata_type = _mode_type(metadata.lstat().st_mode)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise RepositoryError(
                f"Cannot inspect Git metadata boundary {metadata}"
            ) from error
        if metadata_type in {"directory", "regular"}:
            return candidate, metadata
        raise RepositoryError(
            f"Unsafe Git metadata {metadata} (type={metadata_type})"
        )
    return target, None


def _resolve_git_discovery(
    stdout: str,
    candidate_root: Path,
    target: Path,
) -> tuple[Path, Path]:
    fields = stdout.splitlines()
    if len(fields) != 2 or not all(fields):
        raise OSError("Git discovery returned malformed path evidence")
    root = Path(fields[0]).resolve(strict=True)
    git_directory = Path(fields[1]).resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(root)
    if not git_directory.is_dir():
        raise NotADirectoryError(git_directory)
    if root != candidate_root:
        raise OSError(
            "Git worktree root does not match the lexical repository boundary"
        )
    try:
        target.relative_to(root)
    except ValueError as error:
        raise OSError(f"Git root {root} does not contain project path {target}") from error
    return root, git_directory


def _non_git_context(
    target: Path,
    evidence: _FilesystemEvidence,
) -> RepositoryContext:
    errors = tuple(
        f"{label}: exit 128"
        for label in ("git-root", "git-branch", "git-dirty-evidence")
    )
    return RepositoryContext(
        project_path=target,
        is_git_worktree=False,
        branch=None,
        dirty_paths=(),
        instruction_files=evidence.instruction_files,
        language_counts=evidence.language_counts,
        docker_files=evidence.docker_files,
        command_errors=errors,
    )


def _read_filesystem_evidence(root: Path, target: Path) -> _FilesystemEvidence:
    instruction_paths = [Path("AGENTS.md"), Path("SKILLS.md")]
    relative_target = target.relative_to(root)
    current = Path()
    for part in relative_target.parts:
        current /= part
        instruction_paths.append(current / "AGENTS.md")

    instructions = tuple(
        instruction
        for relative in instruction_paths
        if (instruction := _read_optional_instruction(root, relative)) is not None
    )
    docker_names = (
        "Dockerfile",
        "compose.yaml",
        "compose.yml",
        "docker-compose.yaml",
        "docker-compose.yml",
    )
    docker_files = tuple(path for name in docker_names if (path := root / name).is_file())
    language_counts = Counter[str]()
    ignored_directories = {
        ".git",
        ".goalrouter",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "target",
        "venv",
    }
    extension_languages = {
        ".c": "c",
        ".cc": "c++",
        ".cpp": "c++",
        ".h": "c",
        ".hpp": "c++",
        ".py": "python",
        ".rs": "rust",
    }
    for _directory, directories, filenames in root.walk():
        directories[:] = sorted(
            name for name in directories if name not in ignored_directories
        )
        for filename in filenames:
            language = extension_languages.get(Path(filename).suffix.casefold())
            if language is not None:
                language_counts[language] += 1
    return _FilesystemEvidence(
        instruction_files=instructions,
        language_counts=tuple(sorted(language_counts.items())),
        docker_files=docker_files,
    )


def _read_optional_instruction(root: Path, relative: Path) -> InstructionFile | None:
    if relative.is_absolute() or ".." in relative.parts:
        raise RepositoryError(f"Unsafe repository instruction path: {relative}")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    leaf_flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptors: list[int] = []
    primary_error: BaseException | None = None
    try:
        try:
            current = os.open(root, directory_flags)
        except OSError as error:
            raise _unsafe_instruction_error(
                root, relative, _path_type(root)
            ) from error
        descriptors.append(current)
        for part in relative.parts[:-1]:
            try:
                current = os.open(part, directory_flags, dir_fd=current)
            except OSError as error:
                raise _unsafe_instruction_error(
                    root, relative, _entry_type(current, part)
                ) from error
            descriptors.append(current)
        try:
            leaf = os.open(relative.name, leaf_flags, dir_fd=current)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise _unsafe_instruction_error(
                root, relative, _entry_type(current, relative.name)
            ) from error
        descriptors.append(leaf)
        try:
            leaf_type = _mode_type(_fstat_descriptor(leaf).st_mode)
        except OSError as error:
            raise _unsafe_instruction_error(root, relative, "unknown") from error
        if leaf_type != "regular":
            raise _unsafe_instruction_error(root, relative, leaf_type)
        try:
            duplicate = _duplicate_descriptor(leaf)
            descriptors.append(duplicate)
            with _open_descriptor_stream(
                duplicate, "r", encoding="utf-8", closefd=False
            ) as stream:
                content = stream.read()
        except UnicodeError as error:
            raise RepositoryError(
                f"Unsafe repository instruction {root / relative} "
                "(type=regular): invalid UTF-8"
            ) from error
        except OSError as error:
            raise RepositoryError(
                f"Unsafe repository instruction {root / relative} "
                "(type=regular): cannot read safely"
            ) from error
        return InstructionFile(path=root / relative, content=content)
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_error = _close_descriptors(descriptors)
        if cleanup_error is not None:
            message = (
                "Repository instruction descriptor cleanup failed for "
                f"{root / relative}"
            )
            if primary_error is not None:
                primary_error.add_note(f"{message}: {cleanup_error}")
            else:
                raise RepositoryError(message) from cleanup_error


def _mode_type(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "regular"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISBLK(mode):
        return "block-device"
    if stat.S_ISCHR(mode):
        return "character-device"
    return "unknown"


def _path_type(path: Path) -> str:
    try:
        return _mode_type(path.lstat().st_mode)
    except OSError:
        return "unknown"


def _entry_type(directory_fd: int, name: str) -> str:
    try:
        mode = os.stat(name, dir_fd=directory_fd, follow_symlinks=False).st_mode
    except OSError:
        return "unknown"
    return _mode_type(mode)


def _unsafe_instruction_error(
    root: Path, relative: Path, file_type: str
) -> RepositoryError:
    return RepositoryError(
        f"Unsafe repository instruction {root / relative} (type={file_type})"
    )


def _close_descriptors(descriptors: list[int]) -> OSError | None:
    first_error: OSError | None = None
    while descriptors:
        descriptor = descriptors.pop()
        try:
            _close_descriptor(descriptor)
        except OSError as error:
            if first_error is None:
                first_error = error
    return first_error
