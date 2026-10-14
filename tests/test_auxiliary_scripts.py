"""Run wrappers against harmless fixtures, never mail or companion services."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYTHON_WRAPPERS = ["scripts/astra-migrate", "scripts/checkmail"]
SERVICE_WRAPPERS = ["scripts/activity-control.bat", "scripts/embedding-control.bat"]
PYTHON_PROGRAMS = {
    "astra-migrate": "migrate_legacy_astra_state.py",
    "checkmail": "check_163.py",
}
SERVICE_PROGRAMS = {
    "activity-control.bat": "windows-activity.ps1",
    "embedding-control.bat": "start-embedding-windows.ps1",
}
ARGS = ["two words", "中文", "literal!bang", "a & b", "100%literal"]


def _installation(tmp_path: Path, wrapper: str) -> tuple[Path, Path]:
    root = tmp_path / "Astra 中文 & spaces"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    canonical = Path("scripts") / Path(wrapper).name
    shutil.copy2(ROOT / canonical, root / canonical)
    if Path(wrapper) != canonical:
        shutil.copy2(ROOT / wrapper, root / wrapper)
    return root, scripts


def _python_fixture(scripts: Path, program: str) -> None:
    (scripts / program).write_text(
        'import json,os,sys\n'
        'print(json.dumps({"args":sys.argv[1:],"cwd":os.getcwd()}))\n'
        'sys.exit(37)\n', encoding="utf-8",
    )


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="POSIX shell acceptance")
@pytest.mark.parametrize("wrapper", PYTHON_WRAPPERS)
@pytest.mark.parametrize("python_override", [False, True])
def test_posix_helpers_forward_arguments_and_failure_from_another_directory(
    tmp_path: Path, wrapper: str, python_override: bool,
) -> None:
    wrapper += ".sh"
    root, scripts = _installation(tmp_path, wrapper)
    _python_fixture(scripts, PYTHON_PROGRAMS[Path(wrapper).stem])
    env = os.environ.copy()
    env.pop("PYTHON", None)
    if python_override:
        env["PYTHON"] = sys.executable
    else:
        interpreter = root / ".venv/bin/python"
        interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to(sys.executable)
    result = subprocess.run(
        ["bash", str(root / wrapper), *ARGS], cwd=tmp_path, env=env,
        capture_output=True, text=True, check=False, timeout=20,
    )
    assert result.returncode == 37, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"args": ARGS, "cwd": str(root.resolve())}


def _cmd(script: Path, arguments: list[str]) -> str:
    # CMD's /s /c outer quotes enclose a quoted executable plus quoted arguments.
    return 'cmd.exe /d /v:off /s /c "' + " ".join(f'"{value}"' for value in [str(script), *arguments]) + '"'


@pytest.mark.skipif(os.name != "nt", reason="native Windows CMD acceptance")
@pytest.mark.parametrize("wrapper", PYTHON_WRAPPERS)
def test_windows_helpers_forward_arguments_and_failure_from_another_directory(
    tmp_path: Path, wrapper: str,
) -> None:
    wrapper += ".bat"
    root, scripts = _installation(tmp_path, wrapper)
    _python_fixture(scripts, PYTHON_PROGRAMS[Path(wrapper).stem])
    env = {
        **os.environ,
        "PYTHON": str(Path(getattr(sys, "_base_executable", sys.executable)).resolve()),
        "PYTHONUTF8": "1",
    }
    result = subprocess.run(
        _cmd(root / wrapper, ARGS), cwd=tmp_path, env=env,
        capture_output=True, text=True, encoding="utf-8", check=False, timeout=20,
    )
    assert result.returncode == 37, result.stdout + result.stderr
    expected_cwd = root if Path(wrapper).stem == "astra-migrate" else tmp_path
    assert json.loads(result.stdout) == {"args": ARGS, "cwd": str(expected_cwd)}


@pytest.mark.skipif(os.name != "nt", reason="native Windows PowerShell acceptance")
@pytest.mark.parametrize("wrapper", SERVICE_WRAPPERS)
def test_windows_service_helpers_use_sibling_script_and_return_failure(
    tmp_path: Path, wrapper: str,
) -> None:
    root, scripts = _installation(tmp_path, wrapper)
    program = SERVICE_PROGRAMS[Path(wrapper).name]
    receipt = tmp_path / "service-fixture-phases.log"
    (scripts / program).write_text(
        'param([string]$Action)\n'
        '$receipt = $env:ASTRA_WRAPPER_PHASES\n'
        'function Mark-Phase([string]$Name) {\n'
        '  $ticks = [Diagnostics.Stopwatch]::GetTimestamp()\n'
        '  [IO.File]::AppendAllText($receipt, "$Name pid=$PID utc=$([DateTime]::UtcNow.ToString(\'o\')) qpc=$ticks frequency=$([Diagnostics.Stopwatch]::Frequency) highres=$([Diagnostics.Stopwatch]::IsHighResolution)`n")\n'
        '}\n'
        'Mark-Phase "entered action=$Action"\n'
        '[Console]::OutputEncoding = [Text.UTF8Encoding]::new()\n'
        'Mark-Phase "before-json-stdout"\n'
        '@{action=$Action; root=$PSScriptRoot; cwd=$PWD.Path} | ConvertTo-Json -Compress\n'
        'Mark-Phase "after-json-stdout"\n'
        'Mark-Phase "before-exit-29"\n'
        'exit 29\n', encoding="utf-8",
    )
    started_utc = datetime.now(UTC).isoformat()
    started_perf = time.perf_counter()
    try:
        result = subprocess.run(
            _cmd(root / wrapper, ["status"]), cwd=tmp_path,
            env={**os.environ, "ASTRA_WRAPPER_PHASES": str(receipt)},
            capture_output=True, text=True, encoding="utf-8", check=False, timeout=20,
        )
    except subprocess.TimeoutExpired as exc:
        def tail(value):
            return (value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or "")[-4000:]
        diagnostics = {"wrapper": wrapper, "timeout_seconds": 20,
                       "call_started_utc": started_utc,
                       "exception_received_utc": datetime.now(UTC).isoformat(),
                       "call_elapsed_seconds": time.perf_counter() - started_perf,
                       "phases": receipt.read_text(encoding="utf-8") if receipt.exists() else None,
                       "stdout_tail": tail(exc.output), "stderr_tail": tail(exc.stderr)}
        (tmp_path / "service-wrapper-timeout.json").write_text(
            json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        exc.add_note("Service fixture diagnostic: " + json.dumps(diagnostics, ensure_ascii=False))
        raise
    assert result.returncode == 29, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"action": "status", "root": str(scripts), "cwd": str(tmp_path)}


def test_service_timeout_preserves_fixture_phases_and_partial_output(tmp_path, monkeypatch):
    error = subprocess.TimeoutExpired("fixture command", 20, output=b"partial stdout", stderr=b"partial stderr")
    def timeout(command, **kwargs):
        assert kwargs["timeout"] == 20
        Path(kwargs["env"]["ASTRA_WRAPPER_PHASES"]).write_text("entered pid=123 action=status\nbefore-json-stdout\n", encoding="utf-8")
        raise error
    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(subprocess.TimeoutExpired) as captured:
        test_windows_service_helpers_use_sibling_script_and_return_failure(tmp_path, SERVICE_WRAPPERS[0])
    assert captured.value is error
    diagnostics = json.loads((tmp_path / "service-wrapper-timeout.json").read_text(encoding="utf-8"))
    assert diagnostics["phases"].splitlines() == ["entered pid=123 action=status", "before-json-stdout"]
    assert diagnostics["stdout_tail"] == "partial stdout"
    assert diagnostics["stderr_tail"] == "partial stderr"
    assert any("Service fixture diagnostic:" in note for note in error.__notes__)
