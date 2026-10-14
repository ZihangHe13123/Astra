"""Graceful maintenance of the opt-in Windows recorder and its owned children."""

from __future__ import annotations

import argparse
import ctypes
import json
import ntpath
import subprocess
import time

from .common import LauncherError, read_json
from .installation import Installation

MODULE = "agent.runtime.activity_recorder.windows"
IDENTIFIER = "windows-activity-recorder"
TIMEOUT = 45.0


def _processes() -> list[dict]:
    script = ("$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
              "@(Get-CimInstance Win32_Process -Filter \"Name = 'python.exe'\" | "
              "Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine,CreationDate) | "
              "ConvertTo-Json -Compress")
    attempts = 0
    while True:
        attempts += 1
        try:
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
            break
        except subprocess.TimeoutExpired:
            # CIM can be cold during setup. Retry only this read-only query;
            # an unreadable inventory never grants permission to stop/start.
            if attempts == 1:
                continue
            raise LauncherError("Cannot inspect Windows recorder ownership: PowerShell inventory timed out after two attempts.") from None
        except OSError:
            raise LauncherError("Cannot inspect Windows recorder ownership: PowerShell could not start.") from None
    if result.returncode:
        raise LauncherError("Cannot inspect Windows recorder ownership: PowerShell exited unsuccessfully.") from None
    try:
        data = json.loads(result.stdout or "[]")
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
            raise ValueError
        return data
    except ValueError:
        raise LauncherError("Cannot inspect Windows recorder ownership: PowerShell returned invalid process data.") from None


def _split(command: str) -> list[str]:
    # Use the Windows parser, never POSIX shlex or substring process matching.
    from ctypes import wintypes
    loader = getattr(ctypes, "WinDLL")
    shell = loader("shell32", use_last_error=True)
    kernel = loader("kernel32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    count = ctypes.c_int()
    argv = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not argv:
        raise LauncherError("Cannot parse the Windows recorder command.")
    try:
        return [argv[index] for index in range(count.value)]
    finally:
        kernel.LocalFree(ctypes.cast(argv, wintypes.HLOCAL))


def _valid_options(arguments: list[str]) -> None:
    # Only the recorder's existing, non-destructive runtime options are replayed.
    parser = argparse.ArgumentParser(add_help=False, exit_on_error=False, allow_abbrev=False)
    parser.add_argument("--store")
    parser.add_argument("--poll", type=float, default=5)
    parser.add_argument("--idle", type=float, default=120)
    parser.add_argument("--retention-days", type=int, default=30)
    parser.add_argument("--summary-interval", type=float, default=600)
    parser.add_argument("--no-summaries", action="store_true")
    try:
        opts, extra = parser.parse_known_args(arguments)
        if (extra or not 1 <= opts.poll <= 30 or not opts.idle >= opts.poll
                or not 1 <= opts.retention_days <= 365 or not opts.summary_interval >= 60):
            raise ValueError
    except (argparse.ArgumentError, ValueError) as exc:
        raise LauncherError("Recorder uses unsupported startup options; stop it through its original launcher.") from exc


class WindowsServices:
    def __init__(self, install: Installation):
        self.install = install
        # This is the existing recorder's fixed control path, independent of ASTRA_HOME.
        self.folder = install.root / ".astra/windows-activity"

    def _arguments(self, row: dict) -> list[str]:
        if ntpath.normcase(str(row.get("ExecutablePath") or "")) != ntpath.normcase(str(self.install.python)):
            return []
        command = row.get("CommandLine")
        if not isinstance(command, str) or not command:
            return []
        args = _split(command)
        if not args or ntpath.normcase(args[0]) != ntpath.normcase(str(self.install.python)):
            return []
        return args[1:]

    def discover(self) -> list[dict]:
        rows = _processes()
        entries = []
        for row in rows:
            args = self._arguments(row)
            if args[:2] != ["-m", MODULE]:
                continue
            _valid_options(args[2:])
            pid = row.get("ProcessId")
            if type(pid) is not int or pid <= 0 or not row.get("CreationDate"):
                raise LauncherError("Windows recorder identity is incomplete.")
            children = [child["ProcessId"] for child in rows if child.get("ParentProcessId") == pid
                        and self._arguments(child)[:2] == ["-m", "agent.runtime.activity_recorder.summarizer"]]
            identities = [[item["ProcessId"], str(item.get("CreationDate"))] for item in rows
                          if item.get("ProcessId") in {pid, *children}]
            entries.append({"id": IDENTIFIER, "adapter": "windows", "arguments": args,
                            "pids": [pid, *children], "identities": identities})
        if len(entries) > 1:
            raise LauncherError("Multiple Windows recorder processes use this installation; inspect them before updating.")
        return entries

    def validate(self, entry: dict) -> None:
        args = entry.get("arguments")
        if (entry.get("id") != IDENTIFIER or entry.get("adapter") != "windows" or not isinstance(args, list)
                or len(args) > 20 or not all(isinstance(arg, str) and len(arg) <= 4096 and "\x00" not in arg for arg in args)
                or args[:2] != ["-m", MODULE] or not isinstance(entry.get("pids"), list)
                or not entry["pids"] or any(type(pid) is not int or pid <= 0 for pid in entry["pids"])):
            raise LauncherError("Invalid Windows recorder recovery entry.")
        _valid_options(args[2:])

    def stop(self, entry: dict) -> None:
        self.validate(entry)
        current = self.discover()
        if not current:
            return
        if current[0]["arguments"] != entry["arguments"]:
            raise LauncherError("Recorder startup options changed; the new process was left untouched.")
        identities = {tuple(identity) for identity in current[0]["identities"]}
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / "stop").touch()
        deadline = time.monotonic() + TIMEOUT
        while True:
            rows = _processes()
            remaining = {(row.get("ProcessId"), str(row.get("CreationDate"))) for row in rows} & identities
            # Also reject a replacement recorder; do not signal unrelated PIDs.
            recorders = [row for row in rows if self._arguments(row)[:2] == ["-m", MODULE]]
            if not remaining and not recorders:
                return
            if time.monotonic() >= deadline:
                raise LauncherError("Windows recorder is still shutting down. Retry after it exits; no processes were killed.")
            time.sleep(0.25)

    def start(self, entry: dict) -> None:
        self.validate(entry)
        current = self.discover()
        child = None
        if current:
            if current[0]["arguments"] != entry["arguments"]:
                raise LauncherError("A recorder with different options is already running.")
            pid = current[0]["pids"][0]
        else:
            self.folder.mkdir(parents=True, exist_ok=True)
            with (self.folder / "stdout.log").open("ab") as stdout, (self.folder / "stderr.log").open("ab") as stderr:
                try:
                    child = subprocess.Popen([str(self.install.python), *entry["arguments"]], cwd=self.install.root,
                                             stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, close_fds=True,
                                             creationflags=0x00000008 | 0x00000200 | 0x08000000)
                except OSError as exc:
                    raise LauncherError("Could not restart the Windows recorder.") from exc
            pid = child.pid
        deadline = time.monotonic() + TIMEOUT
        while True:
            status = read_json(self.folder / "status.json")
            stamp = status.get("updated_at")
            if (status.get("pid") == pid and not status.get("stopped") and isinstance(stamp, (int, float))
                    and 0 <= time.time() - stamp <= 35 and not (self.folder / "stop").exists()):
                return
            if (child is not None and child.poll() is not None) or time.monotonic() >= deadline:
                raise LauncherError("Windows recorder did not publish a fresh running heartbeat.")
            time.sleep(0.25)
