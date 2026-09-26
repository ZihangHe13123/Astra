"""Exercise the real Bash bootstrap with isolated interpreter candidates."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def bash_command():
    if os.name != "nt":
        return shutil.which("bash")
    git = shutil.which("git")
    candidate = Path(git).parent.parent / "usr/bin/bash.exe" if git else None
    return str(candidate) if candidate and candidate.is_file() else None


def shell_path(path):
    value = path.as_posix()
    return f"/{value[0].lower()}{value[2:]}" if os.name == "nt" else value


def script(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + content, encoding="utf-8", newline="\n")
    path.chmod(0o755)


@pytest.fixture
def bootstrap(tmp_path):
    bash = bash_command()
    if not bash:
        pytest.skip("Bash (Git Bash on Windows) is unavailable")
    root = tmp_path / "Astra 中文 with spaces"
    root.mkdir()
    shutil.copy2(ROOT / "astra.sh", root / "astra.sh")
    binaries = tmp_path / "bin"
    script(binaries / "dirname", 'if [[ "$1" == -- ]]; then shift; fi\nprintf "%s\\n" "${1%/*}"\n')
    return bash, root, binaries


def fake_python(path, label, supported):
    script(path, f'if [[ "${{1:-}}" == -c ]]; then exit {0 if supported else 1}; fi\n'
           f'printf "%s\\n" "selected:{label}" "$PWD" "$@"\nexit 23\n')


def invoke(bootstrap, override=None):
    bash, root, binaries = bootstrap
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHON", "BASH_ENV", "ENV"}}
    if override:
        env["PYTHON"] = shell_path(override)
    return subprocess.run(
        [bash, "--noprofile", "--norc", "-c", 'export PATH="$1"; exec /bin/bash "$2" "${@:3}"',
         "bootstrap", shell_path(binaries), shell_path(root / "astra.sh"),
         "version", "two words", "中文", "literal!bang", "a & b"],
        cwd=root.parent, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
    )


@pytest.mark.parametrize("old_candidate", ["python3", "python3.11"])
def test_unsupported_path_python_falls_back_to_supported_venv(bootstrap, old_candidate):
    _, root, binaries = bootstrap
    fake_python(binaries / old_candidate, "old", False)
    fake_python(root / ".venv/bin/python", "venv", True)
    result = invoke(bootstrap)
    assert result.returncode == 23, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == "selected:venv"
    assert lines[1] == shell_path(root.parent)
    assert lines[3:] == ["version", "two words", "中文", "literal!bang", "a & b"]


def test_old_exact_candidate_does_not_hide_supported_python3(bootstrap):
    _, _, binaries = bootstrap
    fake_python(binaries / "python3.11", "old", False)
    fake_python(binaries / "python3", "new", True)
    result = invoke(bootstrap)
    assert result.returncode == 23, result.stderr
    assert result.stdout.startswith("selected:new\n")


def test_explicit_unsupported_python_is_not_silently_replaced(bootstrap):
    _, root, binaries = bootstrap
    fake_python(binaries / "python3", "new", True)
    explicit = root / "explicit Python"
    fake_python(explicit, "explicit", False)
    result = invoke(bootstrap, explicit)
    assert result.returncode == 1
    assert "Python 3.11 or newer" in result.stderr
    assert "explicit Python" in result.stderr


def test_no_supported_candidate_is_actionable(bootstrap):
    _, _, binaries = bootstrap
    fake_python(binaries / "python3", "old", False)
    result = invoke(bootstrap)
    assert result.returncode == 1
    assert "Python 3.11 or newer" in result.stderr
