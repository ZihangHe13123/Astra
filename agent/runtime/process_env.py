"""Consistent provenance markers for processes spawned by Astra."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping, MutableMapping


# Browser children need OS/GUI configuration, not the agent's model/mail tokens
# or Python/Node/dynamic-library injection settings. Compare case-insensitively
# for Windows, preserving original spellings and values.
_BROWSER_ENV_KEYS = frozenset("""
PATH HOME USER LOGNAME USERNAME USERPROFILE SYSTEMROOT WINDIR SYSTEMDRIVE COMSPEC
PATHEXT PROGRAMFILES PROGRAMFILES(X86) PROGRAMW6432 PROGRAMDATA ALLUSERSPROFILE
APPDATA LOCALAPPDATA HOMEDRIVE HOMEPATH TEMP TMP TMPDIR LANG LANGUAGE TZ
LC_ALL LC_CTYPE LC_NUMERIC LC_TIME LC_COLLATE LC_MONETARY LC_MESSAGES
LC_PAPER LC_NAME LC_ADDRESS LC_TELEPHONE LC_MEASUREMENT LC_IDENTIFICATION
DISPLAY WAYLAND_DISPLAY XAUTHORITY XDG_RUNTIME_DIR XDG_CONFIG_HOME XDG_CACHE_HOME
XDG_DATA_HOME XDG_DATA_DIRS DBUS_SESSION_BUS_ADDRESS DESKTOP_SESSION
XDG_CURRENT_DESKTOP XDG_SESSION_TYPE SESSIONNAME __CF_USER_TEXT_ENCODING
WSL_DISTRO_NAME WSL_INTEROP WSLENV HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY
SSL_CERT_FILE SSL_CERT_DIR SYSTEM_CERTIFICATE_PATH
""".split())


def browser_child_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy the bounded platform environment used by browser/relay children."""
    source = os.environ if environ is None else environ
    result = {key: value for key, value in source.items()
              if key.upper() in _BROWSER_ENV_KEYS}
    for key in list(result):
        if key.upper() == "WSLENV":
            # WSLENV can explicitly export variables across the Windows boundary.
            allowed = {name.upper() for name in result} - {"WSLENV"}
            result[key] = ":".join(item for item in result[key].split(":")
                                   if item.split("/", 1)[0].upper() in allowed)
    return result


def mark_agent_environment(
    env: MutableMapping[str, str] | None = None,
) -> MutableMapping[str, str]:
    """Set Astra attribution without overwriting an outer agent harness."""
    target = os.environ if env is None else env
    target.setdefault("AI_AGENT", "astra")
    target.setdefault("ASTRA_AGENT", "true")
    return target


def pid_alive(pid: int) -> bool:
    """Side-effect-free liveness probe for another process.

    POSIX uses a zero signal (a harmless existence check). Windows must
    *query* the process handle instead: CPython routes ``os.kill`` on Windows
    through ``TerminateProcess`` for non-console signals, so a probe there can
    kill the process it is asking about. Anything uncertain counts as alive so
    callers never clean up state that may still be owned.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        error_invalid_parameter = 87
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _set_winapi_signature(
            kernel32.OpenProcess, [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong], ctypes.c_void_p
        )
        _set_winapi_signature(
            kernel32.GetExitCodeProcess,
            [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)],
            ctypes.c_int,
        )
        _set_winapi_signature(kernel32.CloseHandle, [ctypes.c_void_p], ctypes.c_int)
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            # 只有 ERROR_INVALID_PARAMETER 才是"pid 不存在"；访问被拒等查询失败
            # 无法证明进程已退出，保守视为存活（F3）。
            get_last_error: Callable[[], int] | None = getattr(ctypes, "get_last_error", None)
            error = int(get_last_error()) if callable(get_last_error) else 0
            return error != error_invalid_parameter
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True  # 查询失败 ≠ 已退出：保守视为存活（F3）
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _set_winapi_signature(function, argtypes, restype) -> None:
    """Declare a WinAPI prototype when the object supports one.

    Real ``ctypes`` functions expose ``argtypes``/``restype``; test doubles
    (plain bound methods) do not, and are skipped instead of failing the probe.
    """
    if not hasattr(function, "argtypes"):
        return
    function.argtypes = argtypes
    function.restype = restype


def hidden_process_creationflags(*, new_process_group: bool = False) -> int:
    """Return Windows flags for a console-free child process.

    ``CREATE_NO_WINDOW`` must not be combined with ``DETACHED_PROCESS``;
    Windows explicitly ignores the former in that combination.  Callers that
    need an independently addressable control group can request one without
    invalidating the no-window guarantee.
    """
    if os.name != "nt":
        return 0
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if new_process_group:
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return flags
