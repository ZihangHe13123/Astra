"""Explicit source setup and a dedicated, per-user Astra command."""

from __future__ import annotations

import os
import hashlib
import shlex
import tempfile
from pathlib import Path

from . import dependencies
from .common import LauncherError, git, write_json
from .installation import Installation, user_home
from .locking import exclusive
from .services import ServiceMaintenance
from .transaction import Transaction
from .update import preflight

SHIM_MARKER = "Astra managed source launcher"


def _write_command(target: Path, payload: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".astra-command-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            Path(temporary).chmod(0o755)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _notify_windows_path_change() -> None:
    import ctypes
    sender = getattr(ctypes, "windll").user32.SendMessageTimeoutW
    sender.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_wchar_p,
                       ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_size_t)]
    sender.restype = ctypes.c_ssize_t
    result = ctypes.c_size_t()
    sender(0xFFFF, 0x001A, 0, "Environment", 0x0002, 1000, ctypes.byref(result))


def _legacy_source_shim(root: Path) -> str:
    return f'''#!/usr/bin/env bash
set -euo pipefail

ASTRA_ROOT="{root}"
ASTRA_LAUNCHER="$ASTRA_ROOT/astra.sh"
if [[ ! -x "$ASTRA_LAUNCHER" ]]; then
    printf 'Astra installation not found: %s\\n' "$ASTRA_ROOT" >&2
    exit 1
fi

exec "$ASTRA_LAUNCHER" "$@"'''


def install_command(install: Installation, *, bin_dir: Path | None = None, modify_path: bool = True) -> Path:
    if install.kind != "source":
        raise LauncherError("The package manager already owns this command. Use its installation instructions.")
    destination = bin_dir or (user_home() / "bin" if os.name == "nt" else Path.home() / ".local/bin")
    destination = destination.expanduser().resolve()
    target = destination / ("astra.cmd" if os.name == "nt" else "astra")
    previous = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
    legacy = previous.strip() == _legacy_source_shim(install.root).strip()
    if target.is_symlink() or (target.exists() and SHIM_MARKER not in previous and not legacy):
        raise LauncherError(f"Another installation owns {target}. Choose --bin-dir or remove that installation through its owner.")
    if legacy:
        backup = install.control / "command-backups" / (hashlib.sha256(previous.encode()).hexdigest() + ".sh")
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            backup.write_text(previous, encoding="utf-8")
    bootstrap = str(install.root / ("astra.bat" if os.name == "nt" else "astra.sh"))
    destination.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        bootstrap = bootstrap.replace("%", "%%")
        # Switch before CMD reads the UTF-8 installation path, not only inside
        # astra.bat: a legacy code page otherwise corrupts non-ASCII roots.
        content = f'@echo off\nrem {SHIM_MARKER}\nsetlocal DisableDelayedExpansion\nchcp 65001 >nul\n"{bootstrap}" %*\n'
        _write_command(target, content.replace("\n", "\r\n").encode("utf-8"))
    else:
        _write_command(target, f'#!/bin/sh\n# {SHIM_MARKER}\nexec {shlex.quote(bootstrap)} "$@"\n'.encode("utf-8"))
    if modify_path:
        if os.name == "nt":
            import winreg
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                try:
                    previous, value_type = winreg.QueryValueEx(key, "Path")
                except FileNotFoundError:
                    previous, value_type = "", winreg.REG_EXPAND_SZ
                parts = [item for item in previous.split(";") if item and os.path.normcase(os.path.expandvars(item))
                         != os.path.normcase(str(destination))]
                winreg.SetValueEx(key, "Path", 0, value_type, ";".join([str(destination), *parts]))
            _notify_windows_path_change()
        else:
            shell = Path(os.environ.get("SHELL", "/bin/sh")).name
            profile = Path.home() / (".zshrc" if shell == "zsh" else ".bashrc" if shell == "bash" else ".profile")
            line = f'export PATH={shlex.quote(str(destination))}:"$PATH" # {SHIM_MARKER}'
            profiles = [profile]
            if shell == "bash":
                login = next((Path.home() / name for name in (".bash_profile", ".bash_login", ".profile")
                              if (Path.home() / name).exists()), Path.home() / ".profile")
                profiles.append(login)
            for profile in profiles:
                existing = profile.read_text(encoding="utf-8") if profile.exists() else ""
                if line not in existing.splitlines():
                    with profile.open("a", encoding="utf-8") as stream:
                        stream.write("\n" + line + "\n")
        print("Command installed. Open a new terminal to refresh PATH.", flush=True)
    print(f"Astra command: {target}", flush=True)
    return target


def setup_source(install: Installation, extras: list[str], *, repair: bool = False, gui: bool = False) -> dict:
    if install.kind != "source":
        raise LauncherError("Use the original installer to repair an installed distribution. Source setup cannot modify it.")
    with exclusive(install):
        services = ServiceMaintenance(install)
        preflight(install, allowed_pids=services.allowed_pids)
        dependencies.check_node(install.root, gui=gui or dependencies.gui_enabled(install))
        dependencies.npm_command()
        dependencies.ensure_uv(install)
        selected = dependencies.enabled_extras(install, extras)
        ready = not repair and dependencies.is_ready(install) and selected == install.metadata.get("extras")
        if gui or dependencies.gui_enabled(install):
            ready = ready and dependencies.gui_ready(install)
        if ready:
            try:
                dependencies.python_health(install)
            except LauncherError:
                ready = False
        if not ready:
            try:
                before = git(install.root, "rev-parse", "HEAD")
            except LauncherError:
                before = ""
            transaction = Transaction(install, before, before)
            with services.suspended():
                preflight(install)
                _prepare_environment(install, selected, transaction, gui=gui)
        template, config = install.root / ".env.example", install.root / ".env"
        if template.is_file() and not config.exists():
            # Exclusive creation avoids replacing a concurrently-created configuration.
            with config.open("x", encoding="utf-8") as stream:
                stream.write(template.read_text(encoding="utf-8"))
            print(f"Created {config}. Configure your model provider there; local endpoints can use no API key.")
        result = {"outcome": "ready", "installation": str(install.root), "extras": selected}
        return result if ready else services.finish(result)


def _prepare_environment(install: Installation, selected: list[str], transaction: Transaction, *, gui: bool = False) -> None:
    try:
        transaction.prepare()
        if gui:
            write_json(install.control / "installation.json", {
                **install.metadata, "schema": 1, "root": str(install.root), "gui": True,
            })
        dependencies.synchronize(install, selected)
        dependencies.validate(install)
        dependencies.record_environment(install, selected)
        transaction.commit()
    except BaseException as exc:
        if (install.control / "pending.json").exists():
            try:
                transaction.recover()
            except BaseException as recovery_error:
                raise LauncherError(f"Setup needs recovery: {recovery_error}. Run astra update --recover.") from exc
        raise LauncherError(f"Setup failed; previous generated directories restored. {exc}") from exc
