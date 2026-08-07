# SPDX-License-Identifier: MIT
# File: tests/unit/test_locking.py
# Purpose: Verify cancellation-safe interprocess run and project leases

import asyncio
import errno
import fcntl
import multiprocessing
import os
import stat
import threading
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from multiprocessing.connection import Connection
from pathlib import Path

import pytest

import goalrouter.locking as locking
from goalrouter.errors import ProjectBusyError, RunBusyError, StateError
from goalrouter.locking import (
    FileRunLease,
    ProjectDirectoryWriteLease,
    ProjectWriteLeaseProtocol,
    RunLeaseProtocol,
)
from goalrouter.run_ids import validate_run_id

_PROCESS_TIMEOUT_SECONDS = 10.0


def _accepts_project_protocol(
    lease: ProjectWriteLeaseProtocol,
) -> ProjectWriteLeaseProtocol:
    return lease


def _accepts_run_protocol(lease: RunLeaseProtocol) -> RunLeaseProtocol:
    return lease


def _lease_context(kind: str, path: Path) -> AbstractAsyncContextManager[None]:
    if kind == "project":
        return ProjectDirectoryWriteLease().acquire(path)
    if kind == "run":
        return FileRunLease(path).acquire("run-1")
    raise AssertionError(f"Unknown test lease kind {kind}")


def _holder_process(
    kind: str,
    path: str,
    ready: Connection,
    control: Connection,
) -> None:
    async def hold() -> None:
        async with _lease_context(kind, Path(path)):
            ready.send("held")
            await asyncio.to_thread(control.recv)

    try:
        asyncio.run(hold())
    finally:
        ready.close()
        control.close()


def _contender_process(kind: str, path: str, result: Connection) -> None:
    async def contend() -> str:
        try:
            async with _lease_context(kind, Path(path)):
                return "acquired"
        except (ProjectBusyError, RunBusyError) as error:
            return type(error).__name__

    try:
        result.send(asyncio.run(contend()))
    finally:
        result.close()


def _receive(connection: Connection) -> str:
    if not connection.poll(_PROCESS_TIMEOUT_SECONDS):
        raise AssertionError("Timed out waiting for lease subprocess")
    value = connection.recv()
    if not isinstance(value, str):
        raise AssertionError(f"Unexpected lease subprocess value {value!r}")
    return value


def _join(process: multiprocessing.Process) -> None:
    process.join(_PROCESS_TIMEOUT_SECONDS)
    if process.is_alive():
        process.terminate()
        process.join(_PROCESS_TIMEOUT_SECONDS)
        raise AssertionError("Lease subprocess did not exit")


def _terminate(process: multiprocessing.Process) -> None:
    if process.is_alive():
        process.terminate()
    process.join(_PROCESS_TIMEOUT_SECONDS)
    if process.is_alive():
        process.kill()
        process.join(_PROCESS_TIMEOUT_SECONDS)
    if process.is_alive():
        raise AssertionError("Lease subprocess survived termination")


def _start_holder(
    context: multiprocessing.context.BaseContext,
    kind: str,
    path: Path,
) -> tuple[multiprocessing.Process, Connection, Connection]:
    ready_reader, ready_writer = context.Pipe(duplex=False)
    control_reader, control_writer = context.Pipe(duplex=False)
    process = context.Process(
        target=_holder_process,
        args=(kind, str(path), ready_writer, control_reader),
    )
    process.start()
    ready_writer.close()
    control_reader.close()
    return process, ready_reader, control_writer


def _run_contender(
    context: multiprocessing.context.BaseContext,
    kind: str,
    path: Path,
) -> str:
    result_reader, result_writer = context.Pipe(duplex=False)
    process = context.Process(
        target=_contender_process,
        args=(kind, str(path), result_writer),
    )
    process.start()
    result_writer.close()
    try:
        result = _receive(result_reader)
        _join(process)
        assert process.exitcode == 0
        return result
    finally:
        result_reader.close()
        _terminate(process)
        process.close()


def _prepare_path(kind: str, path: Path) -> None:
    if kind == "project":
        path.mkdir()


async def _acquire_once(kind: str, path: Path) -> None:
    async with _lease_context(kind, path):
        return


def test_concrete_leases_satisfy_structural_protocols(tmp_path: Path) -> None:
    project = ProjectDirectoryWriteLease()
    run = FileRunLease(tmp_path)

    assert _accepts_project_protocol(project) is project
    assert _accepts_run_protocol(run) is run


@pytest.mark.asyncio
async def test_project_directory_lease_rejects_a_second_holder(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    async with ProjectDirectoryWriteLease().acquire(project):
        with pytest.raises(ProjectBusyError):
            async with ProjectDirectoryWriteLease().acquire(project):
                pytest.fail("contender acquired the project lease")


@pytest.mark.asyncio
async def test_run_lease_rejects_a_second_holder(tmp_path: Path) -> None:
    state_root = tmp_path / "state"

    async with FileRunLease(state_root).acquire("run-1"):
        with pytest.raises(RunBusyError):
            async with FileRunLease(state_root).acquire("run-1"):
                pytest.fail("contender acquired the run lease")


@pytest.mark.parametrize(
    ("kind", "busy_name"),
    (("project", "ProjectBusyError"), ("run", "RunBusyError")),
)
def test_independent_process_contention_and_normal_exit_reacquisition(
    tmp_path: Path,
    kind: str,
    busy_name: str,
) -> None:
    path = tmp_path / kind
    _prepare_path(kind, path)
    context = multiprocessing.get_context("spawn")
    holder, ready, control = _start_holder(context, kind, path)
    try:
        assert _receive(ready) == "held"
        assert _run_contender(context, kind, path) == busy_name

        control.send("release")
        _join(holder)
        assert holder.exitcode == 0
        asyncio.run(_acquire_once(kind, path))
    finally:
        ready.close()
        control.close()
        _terminate(holder)
        holder.close()


@pytest.mark.parametrize("kind", ("project", "run"))
def test_forced_process_termination_releases_lease(
    tmp_path: Path,
    kind: str,
) -> None:
    path = tmp_path / kind
    _prepare_path(kind, path)
    context = multiprocessing.get_context("spawn")
    holder, ready, control = _start_holder(context, kind, path)
    try:
        assert _receive(ready) == "held"
        _terminate(holder)

        asyncio.run(_acquire_once(kind, path))
    finally:
        ready.close()
        control.close()
        _terminate(holder)
        holder.close()


@pytest.mark.parametrize("run_id", ("run-1", "A", "a.b_c-2"))
def test_shared_run_id_validator_accepts_safe_identifiers(run_id: str) -> None:
    assert validate_run_id(run_id) == run_id


@pytest.mark.parametrize("run_id", ("", ".hidden", "../escape", "a/b", "/absolute"))
@pytest.mark.asyncio
async def test_file_run_lease_rejects_invalid_run_ids(
    tmp_path: Path,
    run_id: str,
) -> None:
    with pytest.raises(StateError, match="Invalid run ID"):
        async with FileRunLease(tmp_path / "state").acquire(run_id):
            pytest.fail("invalid run ID acquired a lease")


@pytest.mark.asyncio
async def test_run_lock_is_private_stable_and_retained(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    lock_directory = state_root / ".locks"
    run_lock_directory = lock_directory / "runs"
    run_lock_directory.mkdir(parents=True)
    os.chmod(lock_directory, 0o777)
    os.chmod(run_lock_directory, 0o777)
    lock_path = run_lock_directory / "run-1.lock"
    lock_path.write_text("", encoding="utf-8")
    os.chmod(lock_path, 0o666)
    original_inode = lock_path.stat().st_ino

    lease = FileRunLease(state_root)
    async with lease.acquire("run-1"):
        assert stat.S_IMODE(lock_directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(run_lock_directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
        assert lock_path.stat().st_ino == original_inode

    assert lock_path.is_file()
    async with lease.acquire("run-1"):
        assert lock_path.stat().st_ino == original_inode


@pytest.mark.asyncio
async def test_run_lock_rejects_a_symlink_without_touching_its_target(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    lock_directory = state_root / ".locks/runs"
    lock_directory.mkdir(parents=True)
    target = tmp_path / "valuable"
    target.write_text("preserve-me", encoding="utf-8")
    (lock_directory / "run-1.lock").symlink_to(target)

    with pytest.raises(StateError, match="Cannot open run lease"):
        async with FileRunLease(state_root).acquire("run-1"):
            pytest.fail("symlinked run lock acquired")

    assert target.read_text(encoding="utf-8") == "preserve-me"


@pytest.mark.asyncio
async def test_run_lock_rejects_a_non_regular_leaf_without_blocking(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    lock_directory = state_root / ".locks/runs"
    lock_directory.mkdir(parents=True)
    os.mkfifo(lock_directory / "run-1.lock", mode=0o600)

    async with asyncio.timeout(2):
        with pytest.raises(StateError, match="regular file"):
            async with FileRunLease(state_root).acquire("run-1"):
                pytest.fail("non-regular run lock acquired")


def test_run_lock_leaf_open_uses_nonblocking_no_follow_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_open_descriptor = locking._open_descriptor
    lock_leaf_flags: list[int] = []

    def record_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if str(path).endswith(".lock"):
            lock_leaf_flags.append(flags)
        return real_open_descriptor(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(locking, "_open_descriptor", record_open)

    descriptor = locking._open_run_lock(tmp_path / "state", "run-1")
    try:
        assert lock_leaf_flags == [
            os.O_RDWR
            | os.O_CREAT
            | os.O_CLOEXEC
            | os.O_NOFOLLOW
            | os.O_NONBLOCK
        ]
    finally:
        locking._close_descriptor(descriptor)


@pytest.mark.asyncio
async def test_run_lock_rejects_synthetic_device_leaf_without_blocking(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    lock_directory = state_root / ".locks/runs"
    lock_directory.mkdir(parents=True)
    lock_path = lock_directory / "run-1.lock"
    try:
        os.mknod(
            lock_path,
            stat.S_IFCHR | 0o600,
            os.makedev(4095, 1_048_575),
        )
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EINVAL, errno.ENOSYS, errno.EPERM}:
            pytest.skip(f"Synthetic device fixture is unavailable: errno={error.errno}")
        raise

    async with asyncio.timeout(2):
        with pytest.raises(StateError, match=r"Cannot open run lease|regular file"):
            async with FileRunLease(state_root).acquire("run-1"):
                pytest.fail("synthetic device run lock acquired")


@pytest.mark.asyncio
async def test_project_directory_lease_creates_no_target_artifact(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    existing = project / "existing.txt"
    existing.write_text("keep", encoding="utf-8")
    before = tuple(project.iterdir())

    async with ProjectDirectoryWriteLease().acquire(project):
        assert tuple(project.iterdir()) == before

    assert tuple(project.iterdir()) == before


@pytest.mark.parametrize("busy_errno", (errno.EACCES, errno.EAGAIN))
@pytest.mark.asyncio
async def test_only_contention_errnos_map_to_run_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    busy_errno: int,
) -> None:
    real_flock = fcntl.flock

    def fail_lock(_descriptor: int, _operation: int) -> None:
        raise OSError(busy_errno, os.strerror(busy_errno))

    monkeypatch.setattr(locking, "_flock_descriptor", fail_lock)

    assert fcntl.flock is real_flock

    with pytest.raises(RunBusyError):
        async with FileRunLease(tmp_path / "state").acquire("run-1"):
            pytest.fail("injected busy lock acquired")


@pytest.mark.asyncio
async def test_non_contention_lock_error_fails_closed_without_busy_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_flock = fcntl.flock

    def fail_lock(_descriptor: int, _operation: int) -> None:
        raise OSError(errno.EPERM, os.strerror(errno.EPERM))

    monkeypatch.setattr(locking, "_flock_descriptor", fail_lock)

    assert fcntl.flock is real_flock

    with pytest.raises(StateError, match="Cannot acquire run lease") as captured:
        async with FileRunLease(tmp_path / "state").acquire("run-1"):
            pytest.fail("injected invalid lock acquired")

    assert not isinstance(captured.value, RunBusyError)


@pytest.mark.asyncio
async def test_cancellation_during_acquisition_releases_late_descriptor() -> None:
    acquisition_started = threading.Event()
    allow_acquisition = threading.Event()
    released = threading.Event()

    def acquire(
        _open_descriptor: Callable[[], int],
        _busy_error: Callable[[], ProjectBusyError | RunBusyError],
    ) -> int:
        acquisition_started.set()
        if not allow_acquisition.wait(_PROCESS_TIMEOUT_SECONDS):
            raise AssertionError("test did not release acquisition")
        return 41

    def release(descriptor: int) -> None:
        assert descriptor == 41
        released.set()

    async def use_lease() -> None:
        async with locking._exclusive_lease(
            lambda: -1,
            lambda: RunBusyError("busy"),
            acquire_operation=acquire,
            release_operation=release,
            failure_error=lambda error: StateError(str(error)),
        ):
            pytest.fail("cancelled acquisition entered lease body")

    task = asyncio.create_task(use_lease())
    assert await asyncio.to_thread(
        acquisition_started.wait, _PROCESS_TIMEOUT_SECONDS
    )
    task.cancel()
    allow_acquisition.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert released.is_set()


@pytest.mark.asyncio
async def test_repeated_cancellation_during_acquisition_releases_late_descriptor() -> None:
    acquisition_started = threading.Event()
    allow_acquisition = threading.Event()
    acquisition_finished = threading.Event()
    released = threading.Event()

    def acquire(
        _open_descriptor: Callable[[], int],
        _busy_error: Callable[[], ProjectBusyError | RunBusyError],
    ) -> int:
        acquisition_started.set()
        if not allow_acquisition.wait(_PROCESS_TIMEOUT_SECONDS):
            raise AssertionError("test did not release acquisition")
        acquisition_finished.set()
        return 46

    def release(descriptor: int) -> None:
        assert descriptor == 46
        released.set()

    async def use_lease() -> None:
        async with locking._exclusive_lease(
            lambda: -1,
            lambda: RunBusyError("busy"),
            acquire_operation=acquire,
            release_operation=release,
            failure_error=lambda error: StateError(str(error)),
        ):
            pytest.fail("cancelled acquisition entered lease body")

    task = asyncio.create_task(use_lease())
    assert await asyncio.to_thread(
        acquisition_started.wait, _PROCESS_TIMEOUT_SECONDS
    )
    task.cancel("first acquisition cancellation")
    await asyncio.sleep(0)
    task.cancel("second acquisition cancellation")
    await asyncio.sleep(0)
    finished_before_allow = task.done()
    allow_acquisition.set()
    assert await asyncio.to_thread(
        acquisition_finished.wait, _PROCESS_TIMEOUT_SECONDS
    )
    release_observed = await asyncio.to_thread(released.wait, 1.0)

    with pytest.raises(asyncio.CancelledError) as captured:
        await task
    assert not finished_before_allow
    assert release_observed
    assert captured.value.args == ("first acquisition cancellation",)
    assert any(
        "Additional cancellation while waiting for owned lease operation" in note
        and "second acquisition cancellation" in note
        for note in captured.value.__notes__
    )


@pytest.mark.asyncio
async def test_owned_acquisition_task_cancellation_propagates_without_retry() -> None:
    acquisition_calls = 0

    def cancel_acquisition(
        _open_descriptor: Callable[[], int],
        _busy_error: Callable[[], ProjectBusyError | RunBusyError],
    ) -> int:
        nonlocal acquisition_calls
        acquisition_calls += 1
        raise asyncio.CancelledError("owned acquisition cancelled")

    async with asyncio.timeout(2):
        with pytest.raises(asyncio.CancelledError) as captured:
            async with locking._exclusive_lease(
                lambda: -1,
                lambda: RunBusyError("busy"),
                acquire_operation=cancel_acquisition,
                release_operation=lambda _descriptor: None,
                failure_error=lambda error: StateError(str(error)),
            ):
                pytest.fail("cancelled owned acquisition entered lease body")

    assert acquisition_calls == 1
    assert captured.value.args == ("owned acquisition cancelled",)


@pytest.mark.asyncio
async def test_cancellation_during_release_waits_for_cleanup() -> None:
    release_started = threading.Event()
    allow_release = threading.Event()
    release_finished = threading.Event()

    def acquire(
        _open_descriptor: Callable[[], int],
        _busy_error: Callable[[], ProjectBusyError | RunBusyError],
    ) -> int:
        return 42

    def release(descriptor: int) -> None:
        assert descriptor == 42
        release_started.set()
        if not allow_release.wait(_PROCESS_TIMEOUT_SECONDS):
            raise AssertionError("test did not release cleanup")
        release_finished.set()

    async def use_lease() -> None:
        async with locking._exclusive_lease(
            lambda: -1,
            lambda: RunBusyError("busy"),
            acquire_operation=acquire,
            release_operation=release,
            failure_error=lambda error: StateError(str(error)),
        ):
            return

    task = asyncio.create_task(use_lease())
    assert await asyncio.to_thread(release_started.wait, _PROCESS_TIMEOUT_SECONDS)
    task.cancel()
    allow_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert release_finished.is_set()


@pytest.mark.asyncio
async def test_repeated_cancellation_during_release_waits_for_cleanup() -> None:
    release_started = threading.Event()
    allow_release = threading.Event()
    release_finished = threading.Event()

    def acquire(
        _open_descriptor: Callable[[], int],
        _busy_error: Callable[[], ProjectBusyError | RunBusyError],
    ) -> int:
        return 47

    def release(descriptor: int) -> None:
        assert descriptor == 47
        release_started.set()
        if not allow_release.wait(_PROCESS_TIMEOUT_SECONDS):
            raise AssertionError("test did not release cleanup")
        release_finished.set()

    async def use_lease() -> None:
        async with locking._exclusive_lease(
            lambda: -1,
            lambda: RunBusyError("busy"),
            acquire_operation=acquire,
            release_operation=release,
            failure_error=lambda error: StateError(str(error)),
        ):
            return

    task = asyncio.create_task(use_lease())
    assert await asyncio.to_thread(release_started.wait, _PROCESS_TIMEOUT_SECONDS)
    task.cancel("first release cancellation")
    await asyncio.sleep(0)
    task.cancel("second release cancellation")
    await asyncio.sleep(0)
    finished_before_allow = task.done()
    allow_release.set()
    assert await asyncio.to_thread(release_finished.wait, _PROCESS_TIMEOUT_SECONDS)

    with pytest.raises(asyncio.CancelledError) as captured:
        await task
    assert not finished_before_allow
    assert captured.value.args == ("first release cancellation",)
    assert any(
        "Additional cancellation while waiting for owned lease operation" in note
        and "second release cancellation" in note
        for note in captured.value.__notes__
    )


@pytest.mark.asyncio
async def test_cancellation_preserves_cancelled_error_when_release_fails() -> None:
    release_started = threading.Event()
    allow_release = threading.Event()

    def acquire(
        _open_descriptor: Callable[[], int],
        _busy_error: Callable[[], ProjectBusyError | RunBusyError],
    ) -> int:
        return 45

    def fail_release(descriptor: int) -> None:
        assert descriptor == 45
        release_started.set()
        if not allow_release.wait(_PROCESS_TIMEOUT_SECONDS):
            raise AssertionError("test did not release cleanup")
        raise OSError("injected release failure after cancellation")

    async def use_lease() -> None:
        async with locking._exclusive_lease(
            lambda: -1,
            lambda: RunBusyError("busy"),
            acquire_operation=acquire,
            release_operation=fail_release,
            failure_error=lambda error: StateError(str(error)),
        ):
            return

    task = asyncio.create_task(use_lease())
    assert await asyncio.to_thread(release_started.wait, _PROCESS_TIMEOUT_SECONDS)
    task.cancel()
    allow_release.set()

    with pytest.raises(asyncio.CancelledError) as captured:
        await task
    assert any(
        "Lease cleanup failed after cancellation" in note
        and "injected release failure after cancellation" in note
        for note in captured.value.__notes__
    )


@pytest.mark.asyncio
async def test_primary_exception_survives_release_failure() -> None:
    def acquire(
        _open_descriptor: Callable[[], int],
        _busy_error: Callable[[], ProjectBusyError | RunBusyError],
    ) -> int:
        return 43

    def fail_release(_descriptor: int) -> None:
        raise OSError("injected release failure")

    with pytest.raises(ValueError, match="primary failure") as captured:
        async with locking._exclusive_lease(
            lambda: -1,
            lambda: RunBusyError("busy"),
            acquire_operation=acquire,
            release_operation=fail_release,
            failure_error=lambda error: StateError(str(error)),
        ):
            raise ValueError("primary failure")

    assert any(
        "Lease cleanup failed" in note and "injected release failure" in note
        for note in captured.value.__notes__
    )


@pytest.mark.asyncio
async def test_release_failure_without_primary_exception_is_explicit() -> None:
    def acquire(
        _open_descriptor: Callable[[], int],
        _busy_error: Callable[[], ProjectBusyError | RunBusyError],
    ) -> int:
        return 44

    def fail_release(_descriptor: int) -> None:
        raise OSError("injected release failure")

    with pytest.raises(OSError, match="injected release failure"):
        async with locking._exclusive_lease(
            lambda: -1,
            lambda: RunBusyError("busy"),
            acquire_operation=acquire,
            release_operation=fail_release,
            failure_error=lambda error: StateError(str(error)),
        ):
            pass


def test_run_lock_closes_lock_descriptor_when_directory_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_open = os.open
    real_close = os.close
    real_close_descriptors = locking._close_descriptors
    lock_descriptors: list[int] = []
    cleanup_calls = 0

    def record_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if str(path).endswith(".lock"):
            lock_descriptors.append(descriptor)
        return descriptor

    def fail_directory_cleanup(descriptors: list[int]) -> OSError | None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        cleanup_error = real_close_descriptors(descriptors)
        if cleanup_calls == 2:
            assert cleanup_error is None
            return OSError("injected directory cleanup failure")
        return cleanup_error

    monkeypatch.setattr(locking, "_open_descriptor", record_open)
    monkeypatch.setattr(locking, "_close_descriptors", fail_directory_cleanup)

    assert os.open is real_open

    try:
        with pytest.raises(StateError, match="descriptor cleanup failed"):
            locking._open_run_lock(tmp_path / "state", "run-1")
        assert len(lock_descriptors) == 1
        with pytest.raises(OSError) as captured:
            os.fstat(lock_descriptors[0])
        assert captured.value.errno == errno.EBADF
    finally:
        for descriptor in lock_descriptors:
            try:
                real_close(descriptor)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise


def test_private_directory_preserves_chmod_error_when_close_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    real_fchmod = os.fchmod
    real_close = os.close

    def fail_chmod(_descriptor: int, _mode: int) -> None:
        raise OSError("injected chmod failure")

    def fail_child_close(descriptor: int) -> None:
        if descriptor == parent:
            real_close(descriptor)
            return
        real_close(descriptor)
        raise OSError("injected close failure")

    monkeypatch.setattr(locking, "_change_descriptor_mode", fail_chmod)
    monkeypatch.setattr(locking, "_close_descriptor", fail_child_close)

    assert os.fchmod is real_fchmod
    assert os.close is real_close

    try:
        with pytest.raises(OSError, match="injected chmod failure") as captured:
            locking._open_private_directory(parent, "child")
        assert any(
            "Private directory descriptor cleanup failed" in note
            and "injected close failure" in note
            for note in captured.value.__notes__
        )
    finally:
        try:
            real_close(parent)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
