from pathlib import Path
import io
import subprocess
import sys
import tarfile

import pytest

from scripts import phase_t_gate, wheel_smoke


def _source_archive(tmp_path, names):
    path = tmp_path / "source.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name in names:
            member = tarfile.TarInfo(f"example-source/{name}")
            archive.addfile(member, io.BytesIO())
    return path


@pytest.mark.parametrize("relative", [
    ".git", ".env", ".astra/approvals.db", ".logs/backend.log",
    ".astra/skills/personal/private-note/SKILL.md", "personal-notes.md",
    "ui-tui/node_modules/example/index.js", "ui-tui/dist/app.mjs",
    "ui-core/node_modules/typescript/index.js", "ui-core/dist/backend-protocol.js",
    "ui-gui/node_modules/electron/index.js", "ui-gui/dist/main.cjs",
    "ui-gui/test-results/result.json", "ui-gui/playwright-report/index.html",
    "ui-gui/output/screenshot.png", "output/playwright/gui/result.json",
    "ui-core/.cache/compiler.json", "ui-gui/.vite/deps/react.js", "ui-gui/tsconfig.tsbuildinfo",
    "ui-core/.DS_Store", "ui-gui/.ruff_cache/cache", "ui-gui/.mypy_cache/cache",
    "agent/example.pyc",
    "agent/.env.local", "agent/persona.local.json", "agent/private.key",
    "native/appshot-core/.build/private.txt", "docs/superpowers/private.md",
    "docs/validation/private.md", "evals/coding/results/private.json",
])
def test_source_distribution_refuses_private_or_generated_files(tmp_path, relative):
    path = _source_archive(tmp_path, [relative])
    with pytest.raises(SystemExit, match="non-public path"):
        wheel_smoke.check_source_distribution(path)


def _public_source_inputs():
    return [
        "agent/cli/main.py", "agent/runtime/prompts.py", "config/models.yaml",
        "ui-tui/src/index.tsx", "astra.py", "README.md", "pyproject.toml", "PKG-INFO",
        "ui-tui/package.json", "ui-tui/package-lock.json", "ui-tui/tsconfig.json",
        "ui-core/package.json", "ui-core/package-lock.json", "ui-core/tsconfig.json",
        "ui-core/src/backend-protocol.ts", "ui-core/src/backend-handshake.ts", "ui-core/src/types.ts",
        "ui-core/src/session-state.ts", "ui-core/src/agent-team-state.ts", "ui-core/src/turn-changes.ts",
        "ui-gui/package.json", "ui-gui/package-lock.json", "ui-gui/tsconfig.json",
        "ui-gui/build.mjs", "ui-gui/index.html", "ui-gui/src/main/index.ts",
        "ui-gui/src/preload/index.ts", "ui-gui/src/renderer/index.tsx", "ui-gui/src/bridge.ts",
        ".env.example", ".astra/skills/operations/using-computer-use/SKILL.md",
    ]


def test_source_distribution_keeps_public_cli_shared_ui_desktop_and_configuration(tmp_path):
    path = _source_archive(tmp_path, _public_source_inputs())
    wheel_smoke.check_source_distribution(path)


@pytest.mark.parametrize("missing", [
    "ui-core/package-lock.json", "ui-core/src/backend-protocol.ts", "ui-gui/package-lock.json",
    "ui-gui/build.mjs", "ui-gui/src/preload/index.ts", "ui-tui/package-lock.json",
])
def test_source_distribution_requires_shared_ui_and_desktop_build_inputs(tmp_path, missing):
    path = _source_archive(tmp_path, [name for name in _public_source_inputs() if name != missing])
    with pytest.raises(SystemExit, match="missing public inputs") as raised:
        wheel_smoke.check_source_distribution(path)
    assert missing in str(raised.value)


def test_source_distribution_rejects_incomplete_public_inputs(tmp_path):
    path = _source_archive(tmp_path, ["README.md"])
    with pytest.raises(SystemExit, match="missing public inputs"):
        wheel_smoke.check_source_distribution(path)


def test_resolve_tool_paths_uses_windows_virtualenv_layout():
    paths = phase_t_gate.resolve_tool_paths("win32")

    assert paths == {
        "python": phase_t_gate.PROJECT_ROOT / ".venv" / "Scripts" / "python.exe",
        "ruff": phase_t_gate.PROJECT_ROOT / ".venv" / "Scripts" / "ruff.exe",
        "pyright": phase_t_gate.PROJECT_ROOT / ".venv" / "Scripts" / "pyright.exe",
        "npm": "npm.cmd",
    }


def test_resolve_tool_paths_uses_posix_virtualenv_layout():
    paths = phase_t_gate.resolve_tool_paths("darwin")

    assert paths == {
        "python": phase_t_gate.PROJECT_ROOT / ".venv" / "bin" / "python",
        "ruff": phase_t_gate.PROJECT_ROOT / ".venv" / "bin" / "ruff",
        "pyright": phase_t_gate.PROJECT_ROOT / ".venv" / "bin" / "pyright",
        "npm": "npm",
    }
    assert all(isinstance(paths[name], Path) for name in ("python", "ruff", "pyright"))


def test_discover_tui_tests_includes_typescript_and_tsx(tmp_path: Path):
    source = tmp_path / "src"
    nested = source / "components"
    nested.mkdir(parents=True)
    ts_test = source / "plain.test.ts"
    tsx_test = nested / "render.test.tsx"
    ignored = source / "not-a-test.tsx"
    for path in (ts_test, tsx_test, ignored):
        path.write_text("", encoding="utf-8")

    assert phase_t_gate.discover_tui_tests(source) == sorted((ts_test, tsx_test))


def test_normal_gate_uses_external_temporary_storage_and_excludes_performance(monkeypatch):
    calls = []
    monkeypatch.setattr(phase_t_gate, "run", lambda label, command, **kwargs: calls.append((label, command, kwargs)))

    assert phase_t_gate.main([]) == 0
    assert [label for label, _, _ in calls] == [
        "Ruff", "Pyright", "Shared UI build", "Shared UI tests", "TUI tests", "TUI typecheck/build", "Python tests",
    ]
    command = calls[-1][1]
    assert command[command.index("-m", 3) + 1] == "not context_index_performance"
    basetemp = Path(next(value.removeprefix("--basetemp=") for value in command if value.startswith("--basetemp=")))
    assert not basetemp.is_relative_to(phase_t_gate.PROJECT_ROOT / ".astra")
    assert not basetemp.parent.exists()


def test_optional_acceptance_checks_run_serially_with_the_real_performance_flag(monkeypatch):
    calls = []
    monkeypatch.setattr(phase_t_gate.sys, "platform", "darwin")
    monkeypatch.setenv("ASTRA_RUN_CONTEXT_INDEX_PERF", "0")
    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_EMBEDDING", "on")
    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_VECTORS_DB", "/private/user-vectors.sqlite3")
    monkeypatch.setattr(phase_t_gate, "run", lambda label, command, **kwargs: calls.append((label, command, kwargs)))

    assert phase_t_gate.main(["--wheel-smoke", "--context-index-performance", "--native"]) == 0
    assert [label for label, _, _ in calls[-4:]] == [
        "isolated wheel smoke", "Context Index performance", "shared Appshot Swift tests", "macOS Swift tests",
    ]
    performance = calls[-3]
    assert "tests/test_context_index_performance.py" in performance[1]
    assert "-s" in performance[1]
    assert performance[2]["env"]["ASTRA_RUN_CONTEXT_INDEX_PERF"] == "1"
    assert performance[2]["env"]["ASTRA_CONTEXT_INDEX_EMBEDDING"] == "off"
    vectors_path = Path(performance[2]["env"]["ASTRA_CONTEXT_INDEX_VECTORS_DB"])
    performance_temp = Path(next(arg.removeprefix("--basetemp=") for arg in performance[1]
                                 if arg.startswith("--basetemp=")))
    assert vectors_path.parent == performance_temp.parent
    assert not vectors_path.exists()
    assert phase_t_gate.os.environ["ASTRA_RUN_CONTEXT_INDEX_PERF"] == "0"
    assert phase_t_gate.os.environ["ASTRA_CONTEXT_INDEX_EMBEDDING"] == "on"
    assert phase_t_gate.os.environ["ASTRA_CONTEXT_INDEX_VECTORS_DB"] == "/private/user-vectors.sqlite3"
    native = calls[-1][1]
    assert native[:2] == ["swift", "test"]
    assert "--no-parallel" in native and "--disable-automatic-resolution" in native
    assert "--scratch-path" in native
    core = calls[-2][1]
    assert core[:2] == ["swift", "test"]
    assert str(phase_t_gate.PROJECT_ROOT / "native/appshot-core") in core
    assert "--no-parallel" in core
    core_scratch = Path(core[core.index("--scratch-path") + 1])
    assert core_scratch.parent == performance_temp.parent
    assert core_scratch != Path(native[native.index("--scratch-path") + 1])


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_explicit_native_gate_fails_before_other_checks_on_unsupported_platforms(monkeypatch, capsys, platform):
    monkeypatch.setattr(phase_t_gate.sys, "platform", platform)
    calls = []
    monkeypatch.setattr(phase_t_gate, "run", lambda *args, **kwargs: calls.append(args))

    with pytest.raises(SystemExit) as raised:
        phase_t_gate.main(["--native"])
    assert raised.value.code == 2 and calls == []
    assert "--native requires macOS" in capsys.readouterr().err


def test_failed_gate_cleans_temporary_directory_and_does_not_report_success(monkeypatch, capsys):
    temporary_paths = []

    def run(label, command, **kwargs):
        if label == "Python tests":
            temporary_paths.extend(Path(arg.removeprefix("--basetemp=")).parent
                                   for arg in command if arg.startswith("--basetemp="))
            assert temporary_paths[0].is_dir()
            raise SystemExit("Python tests failed")

    monkeypatch.setattr(phase_t_gate, "run", run)
    with pytest.raises(SystemExit, match="Python tests failed"):
        phase_t_gate.main([])
    assert not temporary_paths[0].exists()
    assert "automated gate passed" not in capsys.readouterr().out


@pytest.mark.parametrize("keep_going", [False, True])
def test_failed_stage_only_continues_when_requested_and_gate_still_fails(monkeypatch, capsys, keep_going):
    monkeypatch.setattr(phase_t_gate.sys, "platform", "darwin")
    calls = []
    temporary_paths = []

    def run(label, command, **kwargs):
        calls.append(label)
        if label == "TUI tests":
            raise SystemExit("TUI tests failed with exit code 1")
        if label == "Python tests":
            temporary_paths.extend(Path(arg.removeprefix("--basetemp=")).parent
                                   for arg in command if arg.startswith("--basetemp="))
            assert temporary_paths[0].is_dir()
            raise SystemExit("Python tests failed with exit code 2")

    monkeypatch.setattr(phase_t_gate, "run", run)
    options = ["--wheel-smoke", "--context-index-performance", "--native"]
    if keep_going:
        options.append("--keep-going")
    with pytest.raises(SystemExit) as raised:
        phase_t_gate.main(options)

    assert raised.value.code not in (None, 0)
    if keep_going:
        assert calls == [
            "Ruff", "Pyright", "Shared UI build", "Shared UI tests", "TUI tests", "TUI typecheck/build", "Python tests",
            "isolated wheel smoke", "Context Index performance", "shared Appshot Swift tests", "macOS Swift tests",
        ]
        assert "TUI tests" in str(raised.value) and "Python tests" in str(raised.value)
        assert not temporary_paths[0].exists()
    else:
        assert calls == ["Ruff", "Pyright", "Shared UI build", "Shared UI tests", "TUI tests"]
    assert "real provider 4K smoke" not in calls
    assert "automated gate passed" not in capsys.readouterr().out


def test_keep_going_reports_unavailable_executable_and_checks_remaining_stages(monkeypatch):
    calls = []

    def run(label, command, **kwargs):
        calls.append(label)
        if label == "Ruff":
            raise FileNotFoundError("fixture missing checker")

    monkeypatch.setattr(phase_t_gate, "run", run)
    with pytest.raises(SystemExit, match="Ruff"):
        phase_t_gate.main(["--keep-going"])
    assert calls[-1] == "Python tests"


def test_wheel_smoke_cannot_load_missing_module_from_editable_pth(tmp_path):
    installed = tmp_path / "installed"
    dependencies = tmp_path / "dependencies"
    checkout = tmp_path / "source"
    for directory in (installed, dependencies, checkout):
        directory.mkdir()
    for relative in ("agent/__init__.py", "agent/runtime/__init__.py", "agent/runtime/session_recall.py"):
        module = checkout / relative
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_text("", encoding="utf-8")
    (dependencies / "editable.pth").write_text(str(checkout), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", wheel_smoke.SMOKE_CODE, str(installed), str(dependencies)],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "No module named 'agent'" in result.stderr


def test_wheel_smoke_rejects_a_module_supplied_only_by_dependency_site_packages(tmp_path):
    installed = tmp_path / "installed"
    dependencies = tmp_path / "dependencies"
    installed.mkdir()
    dependencies.mkdir()
    for relative in ("agent/__init__.py", "agent/runtime/__init__.py", "agent/runtime/session_recall.py"):
        module = dependencies / relative
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_text("", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", wheel_smoke.SMOKE_CODE, str(installed), str(dependencies)],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "AssertionError: agent.runtime.session_recall" in result.stderr
