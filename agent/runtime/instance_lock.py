"""Small cross-platform, process-lifetime instance locks."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import BinaryIO


class InstanceAlreadyRunning(RuntimeError):
    """Raised when another process already owns an instance lock."""


class InstanceLock:
    """Hold an advisory lock for as long as the backing file stays open.

    The lock is a kernel-held lock, not a stale PID-file check: a crashed
    process releases it automatically, while a second process cannot race a
    PID check between reading and starting.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        if self._file is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # No buffered or seed writes before ownership: Windows byte locks may
        # cover an empty file while its owner is replacing the PID metadata.
        handle = self.path.open("a+b", buffering=0)
        try:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    # _locking explicitly supports byte ranges beyond EOF.
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                    raise InstanceAlreadyRunning(f"another instance owns {self.path}") from exc
                raise

            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n".encode("ascii"))
            handle.flush()
        except BaseException as exc:
            # Closing also releases a lock already acquired before metadata
            # failed. Keep that original error if close itself reports failure.
            try:
                handle.close()
            except OSError as close_error:
                exc.add_note(f"Closing instance lock also failed: {close_error}")
            raise
        self._file = handle

    def release(self) -> None:
        handle = self._file
        self._file = None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()
