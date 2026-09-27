"""Private Browser Control files, with native Windows and POSIX validation."""
from __future__ import annotations

import os
from pathlib import Path
import stat


def check_directory(path: Path) -> None:
    if os.name == 'nt':
        from .browser_control_windows import check_directory as check
        check(path)
        return
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError('Browser control directory must be user-owned, mode 0700, and not a symlink')


def private_directory(path: Path) -> None:
    if os.name == 'nt':
        from .browser_control_windows import make_directory
        make_directory(path)
    else:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        check_directory(path)


def open_private(path: Path, flags: int, mode: int = 0o600) -> int:
    if os.name == 'nt':
        from .browser_control_windows import open_file
        return open_file(path, flags)
    fd = os.open(path, flags | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0), mode)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError('Browser control file must be private and user-owned, without links')
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_private(path: Path, limit: int = 16384) -> bytes:
    with os.fdopen(open_private(path, os.O_RDONLY), 'rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('Browser control metadata exceeds size limit')
    return data
