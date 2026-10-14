"""Verify runnable backend resources in the actual desktop payload."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys


def test_copied_backend_imports_without_source_checkout(tmp_path):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("desktop_packager", root / "scripts/package_desktop.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target = tmp_path / "backend"
    module.copy_backend(root, target)
    home = tmp_path / "home"
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR"}}
    env.update(PYTHONPATH=str(target), PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1",
               ASTRA_HOME=str(home), ASTRA_ENV_FILE=str(home / ".env"),
               HOME=str(home), USERPROFILE=str(home), LOCALAPPDATA=str(home), APPDATA=str(home),
               AGENT_SESSION_DIR=str(home / "sessions"), AGENT_LOG_DIR=str(home / "logs"))
    result = subprocess.run(
        [sys.executable, "-c",
         "from pathlib import Path; import agent.cli.backend, agent.ui.queries; "
         "from agent.runtime.card_skill import card_skill_content; "
         "assert Path(agent.cli.backend.__file__).is_relative_to(Path.cwd()); "
         "assert 'interactive-cards' in card_skill_content()"],
        cwd=target, env=env, text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
