"""Discover installer-owned browser control; launch only the ordinary browser."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import threading

from .process_env import browser_child_environment

HOST = 'com.astra.browser_control'
BROWSERS = {'edge': ('Microsoft Edge', 'Microsoft Edge.app'),
            'chrome': ('Google/Chrome', 'Google Chrome.app')}


def _registered(browser: str, home: Path, repo: Path) -> bool:
    from .browser_control_install import status
    return status(browser=browser, home=home, repo=repo)['state'] == 'configured'


def windows_browser_executable(browser, *, home=None):
    if os.name != 'nt':
        raise RuntimeError('Registry browser discovery requires native Windows')
    import winreg
    executable = 'msedge.exe' if browser == 'edge' else 'chrome.exe'
    candidates = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            try:
                with winreg.OpenKey(hive, 'Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\' + executable,
                                    0, winreg.KEY_READ | view) as key:
                    value, kind = winreg.QueryValueEx(key, '')
                    if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                        candidates.append(Path(os.path.expandvars(value).strip('"')))
            except FileNotFoundError:
                pass
    relative = ('Microsoft/Edge/Application/' if browser == 'edge' else 'Google/Chrome/Application/') + executable
    for base in (os.environ.get('ProgramFiles'), os.environ.get('ProgramFiles(x86)'),
                 os.environ.get('LOCALAPPDATA', str(Path(home or Path.home()) / 'AppData/Local'))):
        if base:
            candidates.append(Path(base) / relative)
    for path in candidates:
        if path.is_absolute() and path.name.lower() == executable and path.is_file():
            return path
    raise RuntimeError(f'Install {browser.title()} or open it manually before using browser auto-connect')


async def launch_browser(browser: str, *, home: Path | None = None) -> None:
    """Launch/reopen the installed app without CDP or a replacement profile."""
    home = home or Path.home()
    if sys.platform == 'win32':
        app = windows_browser_executable(browser, home=home)
        proc = subprocess.Popen([str(app)], env=browser_child_environment(),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        # A newly opened browser can outlive this runtime. Never kill the user's
        # browser on timeout/cancellation; only reap the launcher when it exits.
        threading.Thread(target=proc.wait, daemon=True).start()
        return
    app_name = BROWSERS[browser][1]
    candidates = [Path('/Applications') / app_name, home / 'Applications' / app_name]
    app = next((path for path in candidates if path.is_dir()), None)
    if app is None:
        raise RuntimeError(f'Install {app_name} before using browser auto-connect')
    proc = await asyncio.create_subprocess_exec(
        '/usr/bin/open', '-g', '-a', str(app),
        env=browser_child_environment(),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(5):
            code = await proc.wait()
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    if code:
        raise RuntimeError(f'Could not launch {app_name}; open the browser and retry')


def auto_browser_options(*, home=None, repo=None, environ=None) -> dict:
    """Installed host enables listening; the extension still requires user opt-in."""
    env = os.environ if environ is None else environ
    mode = env.get('ASTRA_BROWSER_TRANSPORT', '').strip().lower()
    if mode in ('cdp', 'manual'):
        return {'auto_connect': False}
    if mode not in ('', 'auto', 'extension'):
        return {'auto_connect': True, 'setup_error': 'ASTRA_BROWSER_TRANSPORT must be auto, extension, cdp, or manual'}
    home = Path(home or Path.home())
    repo = Path(repo or Path(__file__).resolve().parents[2]).resolve()
    preferred = env.get('ASTRA_BROWSER_APP', '').strip().lower()
    if preferred and preferred not in BROWSERS:
        return {'auto_connect': True, 'setup_error': 'ASTRA_BROWSER_APP must be edge or chrome'}
    if sys.platform in {'darwin', 'win32'}:
        for browser in ([preferred] if preferred else BROWSERS):
            if _registered(browser, home, repo):
                async def launch(selected=browser):
                    await launch_browser(selected, home=home)
                return {'auto_connect': True, 'launch_browser': launch}
    if mode in ('auto', 'extension'):
        return {'auto_connect': True, 'setup_error':
            'Browser auto-connect requires a native host installed for this checkout. '
            'Run astra browser-control install --browser edge --extension-id YOUR_EXTENSION_ID '
            'on Windows or macOS, then enable Auto-connect in the extension.'}
    return {'auto_connect': False}
