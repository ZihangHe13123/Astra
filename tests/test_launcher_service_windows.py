"""Windows service transport and lifecycle, with no real recording or processes."""

from __future__ import annotations

import json
import os
import subprocess
import time
import traceback
from types import SimpleNamespace

import pytest

from agent.launcher import service_windows as mod
from agent.launcher.common import LauncherError, write_json


@pytest.fixture
def recorder(tmp_path, monkeypatch):
    root = tmp_path / "Astra 中文 & spaces!"
    root.mkdir()
    install = SimpleNamespace(root=root, python=root / ".venv/Scripts/python.exe")
    adapter = mod.WindowsServices(install)
    options = ["--store", str(root / "private data/history.db"), "--poll", "1", "--no-summaries"]
    def process(pid=42, *, parent=1, module=mod.MODULE, executable=None, args=None):
        exe = str(executable or install.python)
        return {"ProcessId": pid, "ParentProcessId": parent, "ExecutablePath": exe,
                "CommandLine": json.dumps([exe, "-m", module, *(options if args is None else args)]),
                "CreationDate": f"created-{pid}"}
    rows = [process()]
    monkeypatch.setattr(mod, "_split", json.loads)
    monkeypatch.setattr(mod, "_processes", lambda: list(rows))
    return adapter, rows, process, options


def test_discovery_requires_exact_executable_and_module_and_preserves_arguments(recorder):
    adapter, rows, process, options = recorder
    rows += [process(43, parent=42, module="agent.runtime.activity_recorder.summarizer", args=["--lookback", "90"]),
             process(44, module="agent.cli.backend", args=[]),
             process(45, executable="C:/other/.venv/Scripts/python.exe")]
    entries = adapter.discover()
    assert len(entries) == 1
    assert entries[0]["arguments"] == ["-m", mod.MODULE, *options]
    assert entries[0]["pids"] == [42, 43]


def test_stop_is_cooperative_and_waits_for_owned_summary_child(recorder, monkeypatch):
    adapter, rows, process, _ = recorder
    rows.append(process(43, parent=42, module="agent.runtime.activity_recorder.summarizer", args=[]))
    entry = adapter.discover()[0]
    seen = []
    def processes():
        if (adapter.folder / "stop").exists():
            seen.append("stop observed")
            if len(seen) == 1:
                return [rows[1]]
            return []
        return list(rows)
    monkeypatch.setattr(mod, "_processes", processes)
    monkeypatch.setattr(mod.time, "sleep", lambda _: None)
    adapter.stop(entry)
    assert len(seen) >= 2
    assert (adapter.folder / "stop").is_file()


def test_stop_does_not_wait_for_a_reused_unrelated_pid(recorder, monkeypatch):
    adapter, _, process, _ = recorder
    entry = adapter.discover()[0]
    def processes():
        if (adapter.folder / "stop").exists():
            row = process(42, module="other", args=[])
            row["CreationDate"] = "new process"
            return [row]
        return [process()]
    monkeypatch.setattr(mod, "_processes", processes)
    adapter.stop(entry)


def test_stop_timeout_preserves_pause_settings_and_never_force_kills(recorder, monkeypatch):
    adapter, _, _, _ = recorder
    entry = adapter.discover()[0]
    adapter.folder.mkdir(parents=True)
    (adapter.folder / "pause").write_bytes(b"paused by user")
    monkeypatch.setattr(mod, "TIMEOUT", 0)
    with pytest.raises(LauncherError, match="still shutting down"):
        adapter.stop(entry)
    assert (adapter.folder / "pause").read_bytes() == b"paused by user"


def test_resume_uses_argument_list_and_requires_fresh_heartbeat(recorder, monkeypatch):
    adapter, rows, _, _ = recorder
    entry = adapter.discover()[0]
    rows.clear()
    adapter.folder.mkdir(parents=True)
    (adapter.folder / "pause").touch()
    (adapter.folder / "stop").touch()
    seen = []
    def popen(args, **kwargs):
        seen.append((args, kwargs))
        # Simulate the recorder accepting startup and retaining its pause flag.
        (adapter.folder / "stop").unlink()
        write_json(adapter.folder / "status.json", {"pid": 100, "updated_at": time.time(), "paused": True})
        return SimpleNamespace(pid=100, poll=lambda: None)
    monkeypatch.setattr(mod.subprocess, "Popen", popen)
    adapter.start(entry)
    assert len(seen) == 1
    args, kwargs = seen[0]
    assert args == [str(adapter.install.python), *entry["arguments"]]
    assert kwargs["cwd"] == adapter.install.root
    assert kwargs["creationflags"] & 0x00000008
    assert kwargs["close_fds"] and "shell" not in kwargs
    assert (adapter.folder / "pause").exists()


def test_stale_heartbeat_is_not_success(recorder, monkeypatch):
    adapter, _, _, _ = recorder
    entry = adapter.discover()[0]
    write_json(adapter.folder / "status.json", {"pid": 42, "updated_at": time.time() - 3600})
    monkeypatch.setattr(mod, "TIMEOUT", 0)
    with pytest.raises(LauncherError, match="fresh running heartbeat"):
        adapter.start(entry)


def test_recovery_does_not_duplicate_a_running_recorder(recorder, monkeypatch):
    adapter, _, _, _ = recorder
    entry = adapter.discover()[0]
    write_json(adapter.folder / "status.json", {"pid": 42, "updated_at": time.time()})
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("duplicate launch"))
    adapter.start(entry)


def test_changed_options_are_not_overwritten(recorder):
    adapter, rows, process, _ = recorder
    entry = adapter.discover()[0]
    rows[:] = [process(args=["--poll", "10"])]
    with pytest.raises(LauncherError, match="options changed"):
        adapter.stop(entry)
    assert not (adapter.folder / "stop").exists()


@pytest.mark.parametrize("options", [["--clear-history"], ["--help"], ["--poll", "0"], ["--store"],
                                   ["--idle", "nan"], ["--unknown"], ["--summary-interval", "nan"]])
def test_unknown_or_destructive_options_are_never_replayed(recorder, options):
    adapter, rows, process, _ = recorder
    rows[:] = [process(args=options)]
    with pytest.raises(LauncherError, match="unsupported startup options"):
        adapter.discover()


def test_powershell_process_inventory_is_static_and_explicitly_utf8(monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, '{"ProcessId":42,"CommandLine":"中文 & spaces"}', "")
    monkeypatch.setattr(mod.subprocess, "run", run)
    assert mod._processes()[0]["ProcessId"] == 42
    args, kwargs = calls[0]
    assert "OutputEncoding" in args[-1] and "UTF8Encoding" in args[-1]
    assert kwargs["encoding"] == "utf-8" and "shell" not in kwargs


def test_process_inventory_retries_one_timeout_then_preserves_results(monkeypatch):
    calls = []
    row = {"ProcessId": 42, "CommandLine": "private process arguments"}
    def run(args, **kwargs):
        calls.append(kwargs["timeout"])
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(args, kwargs["timeout"], output="private output")
        return subprocess.CompletedProcess(args, 0, json.dumps([row]), "")
    monkeypatch.setattr(mod.subprocess, "run", run)
    assert mod._processes() == [row]
    assert calls == [15, 15]


@pytest.mark.parametrize("failure,reason,attempts", [
    ("timeout", "timed out", 2),
    ("start", "could not start", 1),
    ("exit", "exited unsuccessfully", 1),
    ("json", "invalid process data", 1),
    ("shape", "invalid process data", 1),
])
def test_inventory_failures_are_bounded_classified_and_do_not_expose_process_data(monkeypatch, failure, reason, attempts):
    secret = "sensitive-command-and-inventory"
    calls = []
    def run(args, **kwargs):
        calls.append(kwargs["timeout"])
        if failure == "timeout":
            raise subprocess.TimeoutExpired([secret], 15, output=secret, stderr=secret)
        if failure == "start":
            raise OSError(secret)
        if failure == "exit":
            return subprocess.CompletedProcess(args, 7, secret, secret)
        return subprocess.CompletedProcess(args, 0, secret if failure == "json" else json.dumps([secret]), "")
    monkeypatch.setattr(mod.subprocess, "run", run)
    with pytest.raises(LauncherError, match=reason) as caught:
        mod._processes()
    assert calls == [15] * attempts
    assert secret not in str(caught.value)
    assert secret not in "".join(traceback.format_exception(caught.value))


@pytest.mark.skipif(os.name != "nt", reason="native Windows command-line parser")
def test_native_windows_commandline_unicode_spaces_and_quotes_roundtrip():
    args = [r"C:\Astra 中文 & spaces!\.venv\Scripts\python.exe", "-m", mod.MODULE,
            "--store", 'C:\\data\\quote" and space\\history.db', "--no-summaries"]
    assert mod._split(subprocess.list2cmdline(args)) == args
