"""Argument transport and isolation for the optional product-debugging CLI."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


SCRIPT = (Path(__file__).resolve().parents[1] / ".astra" / "skills" / "development"
          / "web-product-debug" / "scripts" / "playwright_cli.py")
_SPEC = importlib.util.spec_from_file_location("playwright_skill_launcher", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
launcher = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(launcher)


@pytest.fixture
def recording_npx(tmp_path, monkeypatch):
    """Real Node subprocess records argv without invoking npm or a browser."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the launcher subprocess acceptance test")
    installation = tmp_path / "node installation 中文"
    installation.mkdir()
    capture = tmp_path / "arguments.json"
    recorder = """const fs = require('node:fs');
fs.writeFileSync(process.env.PROBE_CAPTURE, JSON.stringify({argv:process.argv.slice(2),cwd:process.cwd()}));
console.log('stdout-marker'); console.error('stderr-marker');
process.exit(Number(process.env.PROBE_EXIT || '0'));
"""
    if os.name == "nt":
        npx = installation / "npx.cmd"
        npx.write_text("@echo batch-shim-must-not-run\nexit /b 99\n", encoding="utf-8")
        script = installation / "node_modules" / "npm" / "bin" / "npx-cli.js"
        script.parent.mkdir(parents=True)
        script.write_text(recorder, encoding="utf-8")
    else:
        npx = installation / "npx"
        npx.write_text("#!/usr/bin/env node\n" + recorder, encoding="utf-8")
        npx.chmod(0o755)
    monkeypatch.setenv("PATH", str(installation) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("PROBE_CAPTURE", str(capture))
    monkeypatch.setenv("PYTHONUTF8", "1")
    return capture


def invoke(workspace, session, *command, offline=False, check=False):
    args = [sys.executable, str(SCRIPT), "--workspace", str(workspace), "--session", session]
    if offline:
        args.append("--offline")
    if check:
        args.append("--check")
    if command:
        args.extend(["--", *command])
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", check=False)


def test_real_subprocess_preserves_arguments_output_and_exit(recording_npx, tmp_path, monkeypatch):
    workspace = tmp_path / "project space 中文"
    workspace.mkdir()
    monkeypatch.setenv("PROBE_EXIT", "23")
    text = '中文 words "quoted" \'single\' & echo injected | more > out $(echo bad) `echo bad` %PATH% !x! \\end\\'
    result = invoke(workspace, "ui-review", "fill", "e15", text, offline=True)
    assert result.returncode == 23
    assert result.stdout.strip() == "stdout-marker"
    assert result.stderr.strip() == "stderr-marker"
    observed = json.loads(recording_npx.read_text(encoding="utf-8"))
    assert observed["argv"] == ["--yes", "--offline", "--package", launcher.PACKAGE,
                                "playwright-cli", "-s=ui-review", "fill", "e15", text]
    assert Path(observed["cwd"]) == workspace / "output" / "playwright" / "ui-review"
    assert not (Path(observed["cwd"]) / "out").exists()


def test_sessions_and_projects_have_distinct_artifacts(recording_npx, tmp_path):
    observed = []
    for project, session in [("one", "a"), ("one", "b"), ("two", "a"), ("one", "a")]:
        workspace = tmp_path / project
        workspace.mkdir(exist_ok=True)
        assert invoke(workspace, session, "snapshot").returncode == 0
        observed.append(json.loads(recording_npx.read_text(encoding="utf-8")))
    assert len({item["cwd"] for item in observed}) == 3
    assert observed[0] == observed[3]


def test_check_only_reports_paths_without_dispatch_or_writes(recording_npx, tmp_path):
    result = invoke(tmp_path, "preflight", check=True)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["package"] == launcher.PACKAGE
    assert Path(report["cwd"]) == tmp_path / "output" / "playwright" / "preflight"
    assert not recording_npx.exists()
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("session", ["../escape", "a/b", "a\\b", "", "-bad", "UPPER", "con", "lpt1"])
def test_rejects_nonportable_sessions_before_dispatch(recording_npx, tmp_path, session):
    result = invoke(tmp_path, session, "snapshot")
    assert result.returncode == 2
    assert not recording_npx.exists()
    assert not (tmp_path / "output").exists()


def test_requires_explicit_command(recording_npx, tmp_path):
    assert invoke(tmp_path, "empty").returncode == 2
    assert not recording_npx.exists()
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("available, message", [({}, "Node.js"), ({"node": "/node"}, "npx")])
def test_missing_dependency_is_not_dispatched(monkeypatch, tmp_path, capsys, available, message):
    monkeypatch.setattr(launcher.shutil, "which", available.get)
    assert launcher.main(["--workspace", str(tmp_path), "--session", "missing", "--", "snapshot"]) == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "output").exists()


def test_windows_resolves_node_npx_javascript_without_cmd_shell(tmp_path, monkeypatch):
    installation = tmp_path / "Program Files 中文"
    script = installation / "node_modules" / "npm" / "bin" / "npx-cli.js"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    node = str(installation / "node.exe")
    npx = str(tmp_path / "shims" / "npx.cmd")
    monkeypatch.setattr(launcher.shutil, "which", {"node": node, "npx.cmd": npx}.get)
    assert launcher.find_runner("win32") == [node, str(script)]


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_posix_uses_npx_executable(monkeypatch, platform):
    paths = {"node": "/opt/node/bin/node", "npx": "/opt/node/bin/npx"}
    monkeypatch.setattr(launcher.shutil, "which", paths.get)
    assert launcher.find_runner(platform) == [paths["npx"]]


def test_windows_missing_javascript_fails_without_batch_fallback(tmp_path, monkeypatch):
    paths = {"node": str(tmp_path / "node.exe"), "npx.cmd": str(tmp_path / "npx.cmd")}
    monkeypatch.setattr(launcher.shutil, "which", paths.get)
    with pytest.raises(launcher.LauncherError, match="npx-cli.js"):
        launcher.find_runner("win32")


def test_interrupt_never_replays_command(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(launcher, "find_runner", lambda: ["npx"])

    def interrupted(*args, **kwargs):
        calls.append((args, kwargs))
        raise KeyboardInterrupt

    monkeypatch.setattr(launcher.subprocess, "run", interrupted)
    assert launcher.main(["--workspace", str(tmp_path), "--session", "interrupted", "--", "click", "e1"]) == 130
    assert len(calls) == 1
    assert "unknown" in capsys.readouterr().err
