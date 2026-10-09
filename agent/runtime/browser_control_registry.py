"""Per-user Chromium Native Messaging registration; never change policies."""
from __future__ import annotations
import os

HOST_NAME = 'com.astra.browser_control'
KEYS = {'edge': 'Software\\Microsoft\\Edge\\NativeMessagingHosts\\' + HOST_NAME,
        'chrome': 'Software\\Google\\Chrome\\NativeMessagingHosts\\' + HOST_NAME}
_WINDOWS_ONLY = 'The native-host registry requires native Windows'


def require_user_registry():
    """Do not mistake a process-private registry for the ordinary browser's HKCU."""
    if os.name != 'nt':
        raise RuntimeError(_WINDOWS_ONLY)
    import ctypes
    from ctypes import wintypes
    # Agent-launched children may have a virtualized registry even when
    # GetCurrentPackageFullName reports no identity for the Python executable.
    isolated = bool(os.environ.get('CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY', '').strip())
    length = wintypes.UINT()
    query = ctypes.WinDLL('kernel32').GetCurrentPackageFullName
    query.argtypes = [ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR]
    query.restype = wintypes.LONG
    packaged = query(ctypes.byref(length), None) in (0, 122)
    if isolated or packaged:
        raise RuntimeError(
            'Native host registration cannot be verified from this isolated/packaged Windows process. '
            'Run this command in a standalone CMD or PowerShell window outside the agent app; '
            'a local registry read-back does not prove Edge/Chrome can see it.'
        )


def entries(browser) -> list[tuple[str, int, str]]:
    if os.name != 'nt':
        raise RuntimeError(_WINDOWS_ONLY)
    import winreg
    found = []
    for hive_name, hive in [('HKCU', winreg.HKEY_CURRENT_USER), ('HKLM', winreg.HKEY_LOCAL_MACHINE)]:
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            try:
                key = winreg.OpenKey(hive, KEYS[browser], 0, winreg.KEY_READ | view)
            except FileNotFoundError:
                continue
            with key:
                subkeys, values, _ = winreg.QueryInfoKey(key)
                if subkeys or values != 1:
                    raise ValueError('Native host registration contains unexpected data')
                try:
                    value, kind = winreg.QueryValueEx(key, '')
                except FileNotFoundError as exc:
                    raise ValueError('Native host registration has no default manifest path') from exc
                if kind != winreg.REG_SZ:
                    raise ValueError('Native host registration contains unexpected data')
                found.append((hive_name, view, value))
    return found


def matches(browser, manifest):
    values = entries(browser)
    return bool(values) and all(hive == 'HKCU' and value == str(manifest) for hive, _, value in values)


def publish(browser, manifest):
    if os.name != 'nt':
        raise RuntimeError(_WINDOWS_ONLY)
    import winreg
    require_user_registry()
    found = entries(browser)
    if found:
        if matches(browser, manifest):
            return False
        raise FileExistsError('Native host is already registered elsewhere; no registration was overwritten')
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, KEYS[browser], 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
        subkeys, values, _ = winreg.QueryInfoKey(key)
        if subkeys or values:
            raise FileExistsError('Native host registration changed during installation')
        try:
            winreg.QueryValueEx(key, '')
        except FileNotFoundError:
            try:
                winreg.SetValueEx(key, '', 0, winreg.REG_SZ, str(manifest))
            except BaseException:
                # Remove only an empty key created by this operation.
                if winreg.QueryInfoKey(key)[:2] == (0, 0):
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, KEYS[browser])
                raise
        else:
            raise FileExistsError('Native host registration changed during installation')
    return True


def remove(browser, manifest):
    if os.name != 'nt':
        raise RuntimeError(_WINDOWS_ONLY)
    import winreg
    require_user_registry()
    if not entries(browser):
        return
    if not matches(browser, manifest):
        raise PermissionError('Refusing to remove foreign native-host registration')
    # HKCU Software is shared between views. Never write a WOW6432Node alias.
    for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEYS[browser], 0, winreg.KEY_READ | view) as key:
                value, kind = winreg.QueryValueEx(key, '')
                subkeys, values, _ = winreg.QueryInfoKey(key)
                if value != str(manifest) or kind != winreg.REG_SZ or subkeys or values != 1:
                    raise PermissionError('Native host registration changed during removal')
            winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, KEYS[browser], view, 0)
        except FileNotFoundError:
            pass
