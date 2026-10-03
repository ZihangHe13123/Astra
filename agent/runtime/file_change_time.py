"""Metadata-only change tokens for cache invalidation on POSIX and Windows."""
from __future__ import annotations

import ctypes
import os
import sys
from functools import lru_cache
from pathlib import Path


class _FileBasicInfo(ctypes.Structure):
    # Fixed-width types preserve the Win32 ABI in platform-independent tests.
    # https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_basic_info
    _fields_ = [
        ("CreationTime", ctypes.c_int64),
        ("LastAccessTime", ctypes.c_int64),
        ("LastWriteTime", ctypes.c_int64),
        ("ChangeTime", ctypes.c_int64),
        ("FileAttributes", ctypes.c_uint32),
    ]


@lru_cache(maxsize=1)
def _kernel32():
    kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                  ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.GetFileInformationByHandleEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    kernel.GetFileInformationByHandleEx.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    return kernel


def change_time_token(path: Path, info: os.stat_result) -> int:
    """Return a native change-time token, never a content hash or creation time.

    Python's Windows st_ctime reports creation time, so it cannot invalidate a
    same-size rewrite whose mtime was restored. FileBasicInfo.ChangeTime tracks
    metadata changes independently. Values are opaque tokens: POSIX nanoseconds
    and Windows FILETIME ticks need not share an epoch or unit.
    """
    if sys.platform != "win32":
        return info.st_ctime_ns
    kernel = _kernel32()
    # FILE_READ_ATTRIBUTES; allow concurrent readers, writers and replacement.
    handle = kernel.CreateFileW(str(path), 0x80, 0x1 | 0x2 | 0x4, None, 3, 0, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        basic = _FileBasicInfo()
        if not kernel.GetFileInformationByHandleEx(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
            raise ctypes.WinError(ctypes.get_last_error())
        if basic.ChangeTime <= 0:
            raise OSError("The filesystem did not provide a usable file change time")
        return basic.ChangeTime
    finally:
        if not kernel.CloseHandle(handle):
            error = ctypes.get_last_error()
            # Preserve an original query exception while still attempting close.
            if sys.exc_info()[0] is None:
                raise ctypes.WinError(error)
