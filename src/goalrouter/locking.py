# SPDX-License-Identifier: MIT
# File: src/goalrouter/locking.py
# Purpose: Cancellation-safe interprocess run and project write leases

"""Injected Linux file leases for run mutation and project writer ownership."""

import asyncio
import errno
import fcntl
import os
import stat
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from pathlib import Path
from typing import Protocol

from goalrouter.async_tools import prepare_cancellation, wait_for_owned_task
from goalrouter.errors import (
    GoalRouterError,
    ProjectBusyError,
    RepositoryError,
    RunBusyError,
    StateError,
)
from goalrouter.run_ids import validate_run_id

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_RUN_LOCK_FLAGS = (
    os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
)
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600

type _BusyErrorFactory = Callable[[], ProjectBusyError | RunBusyError]
type _FailureErrorFactory = Callable[[OSError], GoalRouterError]
type _DescriptorOpener = Callable[[], int]
type _AcquireOperation = Callable[[_DescriptorOpener, _BusyErrorFactory], int]
type _ReleaseOperation = Callable[[int], None]


class ProjectWriteLeaseProtocol(Protocol):
    """Cross-process exclusive ownership for one physical project writer."""

    def acquire(self, project_path: Path) -> AbstractAsyncContextManager[None]: ...


class RunLeaseProtocol(Protocol):
    """Cross-process exclusive ownership for one persisted run mutator."""

    def acquire(self, run_id: str) -> AbstractAsyncContextManager[None]: ...


class ProjectDirectoryWriteLease:
    """Take a nonblocking exclusive lease on the project directory descriptor."""

    def acquire(self, project_path: Path) -> AbstractAsyncContextManager[None]:
        return _exclusive_lease(
            lambda: _open_project_directory(project_path),
            lambda: ProjectBusyError(f"Project is busy: {project_path}"),
            failure_error=lambda error: RepositoryError(
                f"Cannot acquire project write lease for {project_path}: {error}"
            ),
        )


class FileRunLease:
    """Take a stable nonblocking lockfile lease beneath one state root."""

    def __init__(self, state_root: Path) -> None:
        self._state_root = state_root

    def acquire(self, run_id: str) -> AbstractAsyncContextManager[None]:
        validated = validate_run_id(run_id)
        return _exclusive_lease(
            lambda: _open_run_lock(self._state_root, validated),
            lambda: RunBusyError(f"Run is busy: {validated}"),
            failure_error=lambda error: StateError(
                f"Cannot acquire run lease for {validated}: {error}"
            ),
        )


@asynccontextmanager
async def _exclusive_lease(
    open_descriptor: _DescriptorOpener,
    busy_error: _BusyErrorFactory,
    *,
    failure_error: _FailureErrorFactory,
    acquire_operation: _AcquireOperation | None = None,
    release_operation: _ReleaseOperation | None = None,
) -> AsyncIterator[None]:
    acquire = _acquire_descriptor if acquire_operation is None else acquire_operation
    release = _release_descriptor if release_operation is None else release_operation
    descriptor = await _acquire_cancellation_safe(
        open_descriptor,
        busy_error,
        failure_error,
        acquire,
        release,
    )
    primary_error: BaseException | None = None
    try:
        yield
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            await _release_cancellation_safe(descriptor, release)
        except BaseException as cleanup_error:
            if primary_error is not None:
                primary_error.add_note(f"Lease cleanup failed: {cleanup_error}")
            else:
                raise


async def _acquire_cancellation_safe(
    open_descriptor: _DescriptorOpener,
    busy_error: _BusyErrorFactory,
    failure_error: _FailureErrorFactory,
    acquire_operation: _AcquireOperation,
    release_operation: _ReleaseOperation,
) -> int:
    acquisition = asyncio.create_task(
        asyncio.to_thread(acquire_operation, open_descriptor, busy_error)
    )
    cancellations = await wait_for_owned_task(acquisition)
    try:
        descriptor = acquisition.result()
    except BaseException as acquisition_error:
        if cancellations:
            cancellation = prepare_cancellation(cancellations, operation="lease operation")
            cancellation.add_note(
                f"Lease acquisition completed with an error after cancellation: "
                f"{acquisition_error}"
            )
            raise cancellation from acquisition_error
        if isinstance(acquisition_error, OSError):
            raise failure_error(acquisition_error) from acquisition_error
        raise
    if cancellations:
        cancellation = prepare_cancellation(cancellations, operation="lease operation")
        try:
            await _release_cancellation_safe(descriptor, release_operation)
        except BaseException as cleanup_error:
            cancellation.add_note(
                f"Lease cancellation cleanup failed: {cleanup_error}"
            )
            for note in getattr(cleanup_error, "__notes__", ()):
                cancellation.add_note(f"Lease cancellation cleanup detail: {note}")
        raise cancellation
    return descriptor


async def _release_cancellation_safe(
    descriptor: int,
    release_operation: _ReleaseOperation,
) -> None:
    release = asyncio.create_task(asyncio.to_thread(release_operation, descriptor))
    cancellations = await wait_for_owned_task(release)
    try:
        release.result()
    except BaseException as cleanup_error:
        if cancellations:
            cancellation = prepare_cancellation(cancellations, operation="lease operation")
            cancellation.add_note(
                f"Lease cleanup failed after cancellation: {cleanup_error}"
            )
            raise cancellation from cleanup_error
        raise
    if cancellations:
        raise prepare_cancellation(cancellations, operation="lease operation")


def _acquire_descriptor(
    open_descriptor: _DescriptorOpener,
    busy_error: _BusyErrorFactory,
) -> int:
    descriptor = open_descriptor()
    try:
        _flock_descriptor(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        try:
            _close_descriptor(descriptor)
        except OSError as cleanup_error:
            error.add_note(f"Lease acquisition descriptor cleanup failed: {cleanup_error}")
        if error.errno in {errno.EACCES, errno.EAGAIN}:
            mapped = busy_error()
            for note in getattr(error, "__notes__", ()):
                mapped.add_note(note)
            raise mapped from error
        raise
    return descriptor


def _release_descriptor(descriptor: int) -> None:
    primary_error: OSError | None = None
    try:
        _flock_descriptor(descriptor, fcntl.LOCK_UN)
    except OSError as error:
        primary_error = error
    try:
        _close_descriptor(descriptor)
    except OSError as cleanup_error:
        if primary_error is not None:
            primary_error.add_note(f"Lease descriptor close failed: {cleanup_error}")
        else:
            raise
    if primary_error is not None:
        raise primary_error


def _open_project_directory(project_path: Path) -> int:
    try:
        resolved = project_path.resolve(strict=True)
        return _open_descriptor(resolved, _DIRECTORY_FLAGS)
    except (OSError, RuntimeError) as error:
        raise RepositoryError(
            f"Cannot open project write lease directory {project_path}: {error}"
        ) from error


def _open_run_lock(state_root: Path, run_id: str) -> int:
    descriptors: list[int] = []
    lock_descriptor: int | None = None
    transfer_lock_descriptor = False
    primary_error: BaseException | None = None
    lock_path = state_root / ".locks" / "runs" / f"{run_id}.lock"
    try:
        state_root.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
        root_descriptor = _open_descriptor(state_root, _DIRECTORY_FLAGS)
        descriptors.append(root_descriptor)
        locks_descriptor = _open_private_directory(root_descriptor, ".locks")
        descriptors.append(locks_descriptor)
        runs_descriptor = _open_private_directory(locks_descriptor, "runs")
        descriptors.append(runs_descriptor)
        lock_descriptor = _open_descriptor(
            f"{run_id}.lock",
            _RUN_LOCK_FLAGS,
            _PRIVATE_FILE_MODE,
            dir_fd=runs_descriptor,
        )
        if not stat.S_ISREG(os.fstat(lock_descriptor).st_mode):
            raise StateError(f"Run lease path is not a regular file: {lock_path}")
        _change_descriptor_mode(lock_descriptor, _PRIVATE_FILE_MODE)
        transfer_lock_descriptor = True
        return lock_descriptor
    except OSError as error:
        primary_error = StateError(f"Cannot open run lease {lock_path}: {error}")
        raise primary_error from error
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_error = _close_descriptors(
            [lock_descriptor]
            if lock_descriptor is not None and not transfer_lock_descriptor
            else []
        )
        directory_cleanup_error = _close_descriptors(descriptors)
        if cleanup_error is None:
            cleanup_error = directory_cleanup_error
        elif directory_cleanup_error is not None:
            cleanup_error.add_note(
                f"Run lease directory cleanup also failed: {directory_cleanup_error}"
            )
        if (
            cleanup_error is not None
            and transfer_lock_descriptor
            and lock_descriptor is not None
        ):
            lock_cleanup_error = _close_descriptors([lock_descriptor])
            if lock_cleanup_error is not None:
                cleanup_error.add_note(
                    f"Run lease lock descriptor cleanup also failed: {lock_cleanup_error}"
                )
        if cleanup_error is not None:
            message = f"Run lease descriptor cleanup failed for {lock_path}"
            if primary_error is not None:
                primary_error.add_note(f"{message}: {cleanup_error}")
            else:
                raise StateError(message) from cleanup_error


def _open_private_directory(parent_descriptor: int, name: str) -> int:
    with suppress(FileExistsError):
        os.mkdir(
            name,
            mode=_PRIVATE_DIRECTORY_MODE,
            dir_fd=parent_descriptor,
        )
    descriptor = _open_descriptor(name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
    try:
        _change_descriptor_mode(descriptor, _PRIVATE_DIRECTORY_MODE)
    except BaseException as error:
        try:
            _close_descriptor(descriptor)
        except OSError as cleanup_error:
            error.add_note(
                f"Private directory descriptor cleanup failed: {cleanup_error}"
            )
        raise
    return descriptor


def _close_descriptors(descriptors: list[int]) -> OSError | None:
    primary_error: OSError | None = None
    for descriptor in reversed(descriptors):
        try:
            _close_descriptor(descriptor)
        except OSError as error:
            if primary_error is None:
                primary_error = error
            else:
                primary_error.add_note(f"Additional descriptor close failed: {error}")
    return primary_error


def _close_descriptor(descriptor: int) -> None:
    os.close(descriptor)


def _open_descriptor(
    path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    flags: int,
    mode: int = 0o777,
    *,
    dir_fd: int | None = None,
) -> int:
    return os.open(path, flags, mode, dir_fd=dir_fd)


def _change_descriptor_mode(descriptor: int, mode: int) -> None:
    os.fchmod(descriptor, mode)


def _flock_descriptor(descriptor: int, operation: int) -> None:
    fcntl.flock(descriptor, operation)
