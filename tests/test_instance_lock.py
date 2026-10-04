import asyncio
import errno
import io
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.cli import backend, sessions
from agent.channels.manager import is_address_in_use
from agent.runtime.instance_lock import InstanceAlreadyRunning, InstanceLock
from agent.runtime import instance_lock


def test_instance_lock_is_exclusive_and_reusable(tmp_path: Path):
    path = tmp_path / ".astra" / "backend.lock"
    first = InstanceLock(path)
    second = InstanceLock(path)

    first.acquire()
    try:
        with pytest.raises(InstanceAlreadyRunning):
            second.acquire()
    finally:
        first.release()

    assert path.read_text(encoding="ascii").strip() == str(os.getpid())
    second.acquire()
    second.release()


def test_backend_run_allows_multiple_cli_instances(monkeypatch):
    calls = []

    async def fake_main():
        calls.append(True)

    monkeypatch.setattr(backend, "main", fake_main)
    assert backend.run() == 0
    assert backend.run() == 0
    assert calls == [True, True]


def test_startup_session_path_is_process_scoped(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sessions, "SESSION_DIR", tmp_path)
    monkeypatch.delenv("AGENT_SESSION", raising=False)
    monkeypatch.setattr(sessions.os, "getpid", lambda: 101)
    first = sessions.startup_session_path()
    monkeypatch.setattr(sessions.os, "getpid", lambda: 202)
    second = sessions.startup_session_path()

    assert first != second
    assert first.stem.endswith("_101")
    assert second.stem.endswith("_202")


def test_channel_port_conflict_only_disables_optional_channel(capsys):
    class ConflictingChannelManager:
        async def start(self):
            raise OSError(10048, "address already in use")

    conflict = asyncio.run(backend._start_channel_manager(ConflictingChannelManager()))

    assert isinstance(conflict, OSError)
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


@pytest.mark.parametrize("errno", [48, 98, 10048])
def test_backend_recognizes_address_in_use(errno):
    assert is_address_in_use(OSError(errno, "address in use"))


@pytest.fixture
def windows_lock_io(tmp_path, monkeypatch):
    """Model mandatory byte locking, including another owner's empty-file window."""
    events = []
    state = {"held": False, "error": None}

    class File(io.BytesIO):
        def fileno(self):
            return 42

        def write(self, value):
            events.append(("write", value))
            if not state["held"]:
                raise PermissionError(errno.EACCES, "write before owning the byte lock")
            return super().write(value)

        def truncate(self, size=None):
            events.append(("truncate", size))
            assert state["held"]
            return super().truncate(size)

        def close(self):
            events.append(("close",))
            state["held"] = False
            super().close()

    handle = File()

    def open_file(mode, **options):
        events.append(("open", mode, options))
        return handle

    def locking(fd, mode, count):
        events.append(("lock", mode))
        assert fd == 42 and count == 1 and handle.tell() == 0
        if mode == 1:
            if state["error"] is not None:
                raise state["error"]
            state["held"] = True
        else:
            assert mode == 2 and state["held"]
            state["held"] = False

    lock = InstanceLock(tmp_path / "instance.lock")
    monkeypatch.setattr(instance_lock, "os", SimpleNamespace(name="nt", getpid=lambda: 123, SEEK_END=os.SEEK_END))
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(locking=locking, LK_NBLCK=1, LK_UNLCK=2))
    monkeypatch.setattr(Path, "open", lambda _path, mode, **options: open_file(mode, **options))
    return lock, handle, state, events


def test_windows_empty_lock_acquires_kernel_range_before_any_write(windows_lock_io):
    lock, handle, state, events = windows_lock_io
    lock.acquire()
    try:
        assert events[:2] == [("open", "a+b", {"buffering": 0}), ("lock", 1)]
        assert handle.getvalue() == b"123\n"
        assert all(event != ("write", b"\0") for event in events)
        assert state["held"]
    finally:
        lock.release()
    assert events[-2:] == [("lock", 2), ("close",)]
    assert handle.closed


def test_windows_contender_never_writes_during_owner_truncation(windows_lock_io):
    lock, handle, state, events = windows_lock_io
    conflict = PermissionError(errno.EACCES, "byte zero is owned beyond EOF")
    state["error"] = conflict
    with pytest.raises(InstanceAlreadyRunning) as caught:
        lock.acquire()
    assert caught.value.__cause__ is conflict
    assert events == [("open", "a+b", {"buffering": 0}), ("lock", 1), ("close",)]
    assert handle.closed and lock._file is None


@pytest.mark.parametrize("code", [errno.EBADF, errno.EIO, errno.EINVAL])
def test_lock_system_errors_are_not_reported_as_contention(windows_lock_io, code):
    lock, handle, state, _ = windows_lock_io
    error = OSError(code, "real lock system failure")
    state["error"] = error
    with pytest.raises(OSError) as caught:
        lock.acquire()
    assert caught.value is error
    assert handle.closed and lock._file is None


@pytest.mark.parametrize("operation", ["truncate", "write", "flush"])
@pytest.mark.parametrize("close_fails", [False, True])
def test_metadata_failure_closes_kernel_lock_and_preserves_error(tmp_path, monkeypatch, operation, close_fails):
    path = tmp_path / "metadata.lock"
    original_open = Path.open
    handles = []
    error = OSError(errno.ENOSPC, "metadata write failed")

    class FailingMetadata:
        def __init__(self, handle):
            self.handle = handle

        def __getattr__(self, name):
            if name == "close" and close_fails:
                def fail_close():
                    self.handle.close()
                    raise OSError(errno.EIO, "close reported another failure")
                return fail_close
            if name == operation:
                def fail(*_args, **_kwargs):
                    raise error
                return fail
            return getattr(self.handle, name)

    def open_file(target, *args, **kwargs):
        handle = original_open(target, *args, **kwargs)
        if target == path:
            handles.append(handle)
            return FailingMetadata(handle)
        return handle

    lock = InstanceLock(path)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", open_file)
        with pytest.raises(OSError) as caught:
            lock.acquire()
    assert caught.value is error
    if close_fails:
        assert any("close reported another failure" in note for note in error.__notes__)
    assert handles[0].closed and lock._file is None
    # This is a real kernel lock, so successful reuse proves failure cleanup
    # releases ownership instead of merely clearing a Python bookkeeping flag.
    lock.acquire()
    lock.release()
